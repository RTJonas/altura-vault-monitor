#!/usr/bin/env python3
"""
Altura Vault Monitor (HyperEVM) -> Telegram

Vigila el vault de Altura y avisa por Telegram ante cualquier evento relevante:
  - Caida del PPS (price-per-share)  -> en un vault market-neutral NUNCA deberia bajar
  - PPS estancado / oracle stale     -> el reporter dejo de actualizar
  - Vault en pausa (Paused)          -> los retiros pueden estar bloqueados
  - Cambio de roles de gobernanza    -> Guardian / Operator / Admin / Timelock
  - Caida brusca del TVL             -> posible corrida / unwind de estrategias
  - Heartbeat diario                 -> confirma que el monitor sigue vivo

Disenado para correr 24/7 en la nube (Docker / systemd / Railway / Fly).
Toda la config va por variables de entorno (no hay secretos en el codigo).
"""

import os
import sys
import json
import time
import signal
import logging
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from web3 import Web3

# ----------------------------------------------------------------------------
# Configuracion (todo por env vars; valores por defecto razonables)
# ----------------------------------------------------------------------------
VAULT_ADDRESS   = os.getenv("VAULT_ADDRESS", "0xd0Ee0CF300DFB598270cd7F4D0c6E0D8F6e13f29")
RPC_URL         = os.getenv("RPC_URL", "https://rpc.hyperliquid.xyz/evm")
BOT_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_SECONDS    = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))   # cada cuanto consulta
PPS_DROP_BPS    = float(os.getenv("PPS_DROP_BPS", "30"))           # alerta si PPS cae >= 0.30% del maximo
PPS_STALE_HOURS = float(os.getenv("PPS_STALE_HOURS", "12"))        # warn si PPS no sube en X horas
TVL_DROP_PCT    = float(os.getenv("TVL_DROP_PCT", "10"))           # alerta si TVL cae >= X% entre ciclos
HEARTBEAT_HOURS = float(os.getenv("HEARTBEAT_HOURS", "24"))        # 0 = desactivar
STATE_FILE      = os.getenv("STATE_FILE", "altura_monitor_state.json")
RPC_FAIL_ALERT  = int(os.getenv("RPC_FAIL_ALERT", "5"))            # avisa tras N fallos RPC seguidos

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("altura")

# ----------------------------------------------------------------------------
# ABI minima: solo lo que necesitamos (ERC-4626 + AccessControl + Pausable)
# ----------------------------------------------------------------------------
VAULT_ABI = [
    {"name": "convertToAssets", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "shares", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "totalAssets", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "totalSupply", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "asset", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "paused", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "bool"}]},
    # AccessControlEnumerable -> permite listar quien tiene cada rol
    {"name": "getRoleMemberCount", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "role", "type": "bytes32"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "getRoleMember", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "role", "type": "bytes32"}, {"name": "index", "type": "uint256"}],
     "outputs": [{"name": "", "type": "address"}]},
    # Eventos
    {"anonymous": False, "name": "RoleGranted", "type": "event", "inputs": [
        {"indexed": True, "name": "role", "type": "bytes32"},
        {"indexed": True, "name": "account", "type": "address"},
        {"indexed": True, "name": "sender", "type": "address"}]},
    {"anonymous": False, "name": "RoleRevoked", "type": "event", "inputs": [
        {"indexed": True, "name": "role", "type": "bytes32"},
        {"indexed": True, "name": "account", "type": "address"},
        {"indexed": True, "name": "sender", "type": "address"}]},
    {"anonymous": False, "name": "Paused", "type": "event",
     "inputs": [{"indexed": False, "name": "account", "type": "address"}]},
    {"anonymous": False, "name": "Unpaused", "type": "event",
     "inputs": [{"indexed": False, "name": "account", "type": "address"}]},
]

# Getters bytes32 candidatos -> los probamos para poner etiqueta legible a cada rol.
# Si alguno no existe, se ignora. La DETECCION de cambios no depende de esto.
ROLE_GETTERS = [
    "DEFAULT_ADMIN_ROLE", "GUARDIAN_ROLE", "OPERATOR_ROLE",
    "ORACLE_TIMELOCK_ROLE", "TIMELOCK_ROLE", "ORACLE_ROLE", "PAUSER_ROLE",
]
ROLE_GETTER_ABI = [
    {"name": n, "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "bytes32"}]}
    for n in ROLE_GETTERS
]
ERC20_DECIMALS_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]}
]


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def tg_send(text: str):
    """Envia un mensaje a Telegram. No tumba el proceso si falla."""
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("Telegram no configurado; mensaje: %s", text)
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={
                "chat_id": CHAT_ID, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=15)
            if r.ok:
                return
            log.error("Telegram %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.error("Telegram error (intento %s): %s", attempt + 1, e)
        time.sleep(2)


# ----------------------------------------------------------------------------
# Estado persistente (sobrevive reinicios en la nube)
# ----------------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error("No se pudo guardar el estado: %s", e)


# ----------------------------------------------------------------------------
# Helpers de lectura on-chain
# ----------------------------------------------------------------------------
def build_role_map(w3, vault_addr) -> dict:
    """Devuelve {role_hash_hex: 'NOMBRE'} probando los getters candidatos."""
    c = w3.eth.contract(address=vault_addr, abi=ROLE_GETTER_ABI)
    out = {}
    for name in ROLE_GETTERS:
        try:
            h = getattr(c.functions, name)().call()
            out[h.hex()] = name
        except Exception:
            pass
    return out

def snapshot_roles(vault, role_map: dict) -> dict:
    """Snapshot {role_hash: [holders]} via AccessControlEnumerable (best-effort)."""
    snap = {}
    for role_hex in role_map.keys():
        role_bytes = bytes.fromhex(role_hex)
        try:
            n = vault.functions.getRoleMemberCount(role_bytes).call()
            holders = [vault.functions.getRoleMember(role_bytes, i).call() for i in range(n)]
            snap[role_hex] = sorted(h.lower() for h in holders)
        except Exception:
            # Contrato no enumerable: nos apoyamos solo en los eventos
            pass
    return snap


# ----------------------------------------------------------------------------
# Monitor principal
# ----------------------------------------------------------------------------
class Monitor:
    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
        # Algunas chains tipo HyperEVM necesitan el middleware PoA; lo inyectamos
        # tolerando las diferencias entre web3 v6 y v7.
        try:
            from web3.middleware import geth_poa_middleware  # web3 v6
            self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        except Exception:
            try:
                from web3.middleware import ExtraDataToPOAMiddleware  # web3 v7
                self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            except Exception:
                pass

        if not self.w3.is_connected():
            raise SystemExit(f"No se pudo conectar al RPC: {RPC_URL}")

        self.vault_addr = Web3.to_checksum_address(VAULT_ADDRESS)
        self.vault = self.w3.eth.contract(address=self.vault_addr, abi=VAULT_ABI)

        self.share_dec = self.vault.functions.decimals().call()
        try:
            asset_addr = self.vault.functions.asset().call()
            asset = self.w3.eth.contract(address=asset_addr, abi=ERC20_DECIMALS_ABI)
            self.asset_dec = asset.functions.decimals().call()
        except Exception:
            self.asset_dec = 6  # USDT0 suele ser 6; fallback razonable

        self.role_map = build_role_map(self.w3, self.vault_addr)
        log.info("Roles detectados: %s", list(self.role_map.values()) or "(ninguno legible)")

        self.state = load_state()
        self.rpc_fails = 0
        self.last_alert = {}  # cooldown para avisos repetibles (stale, rpc)

    # ---- lecturas ----
    def read_pps(self) -> float:
        one_share = 10 ** self.share_dec
        assets = self.vault.functions.convertToAssets(one_share).call()
        return assets / (10 ** self.asset_dec)

    def read_tvl(self) -> float:
        return self.vault.functions.totalAssets().call() / (10 ** self.asset_dec)

    def read_paused(self) -> bool:
        try:
            return bool(self.vault.functions.paused().call())
        except Exception:
            return False

    # ---- utilidades de alerta ----
    def cooldown_ok(self, key: str, hours: float) -> bool:
        last = self.last_alert.get(key, 0)
        if time.time() - last >= hours * 3600:
            self.last_alert[key] = time.time()
            return True
        return False

    def fmt_addr(self, a: str) -> str:
        return f"<code>{a}</code>"

    # ---- chequeo de eventos de gobernanza / pausa entre bloques ----
    def scan_events(self, from_block: int, to_block: int):
        if from_block > to_block:
            return
        for ev_name in ("RoleGranted", "RoleRevoked", "Paused", "Unpaused"):
            try:
                logs = getattr(self.vault.events, ev_name)().get_logs(
                    from_block=from_block, to_block=to_block)
            except TypeError:
                logs = getattr(self.vault.events, ev_name)().get_logs(
                    fromBlock=from_block, toBlock=to_block)
            except Exception as e:
                log.error("getLogs %s fallo: %s", ev_name, e)
                continue
            for lg in logs:
                self.handle_event(ev_name, lg)

    def handle_event(self, name: str, lg):
        tx = lg["transactionHash"].hex()
        if name in ("Paused", "Unpaused"):
            emoji = "🛑" if name == "Paused" else "✅"
            tg_send(f"{emoji} <b>VAULT {name.upper()}</b>\n"
                    f"El estado de pausa del vault cambio.\n"
                    f"tx: {self.fmt_addr(tx)}")
            return
        role_hex = lg["args"]["role"].hex()
        label = self.role_map.get(role_hex, f"rol {role_hex[:10]}…")
        account = lg["args"]["account"]
        verb = "OTORGADO a" if name == "RoleGranted" else "REVOCADO de"
        tg_send(f"🚨 <b>CAMBIO DE ROL: {label}</b>\n"
                f"{verb} {self.fmt_addr(account)}\n"
                f"tx: {self.fmt_addr(tx)}\n"
                f"⚠️ Revisa de inmediato si es un cambio esperado.")

    # ---- diff de holders (cubre huecos si el monitor estuvo caido) ----
    def check_role_snapshot(self):
        if not self.role_map:
            return
        current = snapshot_roles(self.vault, self.role_map)
        if not current:
            return
        prev = self.state.get("role_snapshot", {})
        if prev and current != prev:
            for role_hex, holders in current.items():
                if holders != prev.get(role_hex):
                    label = self.role_map.get(role_hex, role_hex[:10])
                    tg_send(f"🚨 <b>HOLDERS DEL ROL {label} CAMBIARON</b>\n"
                            f"antes: {prev.get(role_hex)}\n"
                            f"ahora: {holders}\n"
                            f"⚠️ Cambio de gobernanza detectado.")
        self.state["role_snapshot"] = current

    # ---- ciclo principal ----
    def poll(self):
        block = self.w3.eth.block_number
        pps = self.read_pps()
        tvl = self.read_tvl()
        paused = self.read_paused()
        now = time.time()

        prev_pps = self.state.get("last_pps")
        high_pps = max(self.state.get("high_pps", 0.0), pps)
        prev_tvl = self.state.get("last_tvl")
        last_block = self.state.get("last_block", block)

        log.info("block=%s  PPS=%.8f  TVL=%.2f  paused=%s", block, pps, tvl, paused)

        # 1) Pausa activa
        if paused and self.cooldown_ok("paused", 1):
            tg_send("🛑 <b>VAULT EN PAUSA</b>\n"
                    "El vault esta pausado: los retiros pueden estar bloqueados.\n"
                    "Verifica los canales oficiales YA.")

        # 2) Caida del PPS (vs ciclo anterior y vs maximo de sesion)
        if prev_pps is not None:
            if pps < prev_pps * (1 - 1e-6):  # baja respecto al ultimo: anomalo
                tg_send(f"🔴 <b>PPS BAJO</b>\n"
                        f"{prev_pps:.8f} → {pps:.8f}\n"
                        f"En un vault market-neutral el PPS no deberia bajar. "
                        f"Considera retirar.")
            drop_bps = (high_pps - pps) / high_pps * 10000 if high_pps else 0
            if drop_bps >= PPS_DROP_BPS:
                tg_send(f"🔴 <b>PPS {drop_bps:.0f} bps por debajo del maximo</b>\n"
                        f"max={high_pps:.8f}  actual={pps:.8f}\n"
                        f"Posible drawdown. Evalua salida.")

        # 3) PPS estancado (oracle stale)
        if prev_pps is not None and pps > prev_pps:
            self.state["last_increase_ts"] = now
        last_inc = self.state.get("last_increase_ts", now)
        if not paused and (now - last_inc) > PPS_STALE_HOURS * 3600:
            if self.cooldown_ok("stale", PPS_STALE_HOURS):
                hrs = (now - last_inc) / 3600
                tg_send(f"🟡 <b>PPS sin actualizar hace {hrs:.1f} h</b>\n"
                        f"El oracle reporter podria estar caido o congelado.")

        # 4) Caida brusca del TVL
        if prev_tvl and prev_tvl > 0:
            tvl_drop = (prev_tvl - tvl) / prev_tvl * 100
            if tvl_drop >= TVL_DROP_PCT:
                tg_send(f"🟠 <b>TVL cayo {tvl_drop:.1f}%</b>\n"
                        f"{prev_tvl:,.0f} → {tvl:,.0f}\n"
                        f"Posible corrida o unwind de estrategias.")

        # 5) Eventos de gobernanza/pausa + diff de holders
        self.scan_events(last_block + 1, block)
        self.check_role_snapshot()

        # 6) Heartbeat diario
        if HEARTBEAT_HOURS > 0:
            last_hb = self.state.get("last_heartbeat", 0)
            if now - last_hb >= HEARTBEAT_HOURS * 3600:
                tg_send(f"💙 <b>Altura monitor OK</b>\n"
                        f"PPS={pps:.6f}  TVL={tvl:,.0f}  paused={paused}\n"
                        f"block={block}")
                self.state["last_heartbeat"] = now

        # Persistir
        self.state.update({
            "last_pps": pps, "high_pps": high_pps,
            "last_tvl": tvl, "last_block": block,
        })
        save_state(self.state)

    def run(self):
        tg_send("🟢 <b>Altura monitor iniciado</b>\n"
                f"vault: {self.fmt_addr(self.vault_addr)}\n"
                f"intervalo: {POLL_SECONDS}s")
        while True:
            try:
                self.poll()
                self.rpc_fails = 0
            except Exception as e:
                self.rpc_fails += 1
                log.error("Error en ciclo (%s seguidos): %s", self.rpc_fails, e)
                if self.rpc_fails == RPC_FAIL_ALERT:
                    tg_send(f"⚠️ <b>Monitor con problemas de RPC</b>\n"
                            f"{self.rpc_fails} fallos seguidos: <code>{e}</code>")
            time.sleep(POLL_SECONDS)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID: corre en modo log-only.")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Monitor().run()


if __name__ == "__main__":
    main()
