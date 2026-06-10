# Altura Vault Monitor → Telegram

Monitor 24/7 del vault de Altura en HyperEVM. Te avisa por Telegram ante:

| Alerta | Disparador | Severidad |
|---|---|---|
| PPS bajo | el price-per-share cae vs. el ciclo anterior o vs. el máximo | 🔴 crítico |
| PPS lejos del máximo | caída ≥ `PPS_DROP_BPS` (def. 0.30%) | 🔴 |
| Oracle stale | el PPS no sube hace ≥ `PPS_STALE_HOURS` (def. 12h) | 🟡 |
| TVL en caída | `totalAssets` baja ≥ `TVL_DROP_PCT` (def. 10%) entre ciclos | 🟠 |
| Vault en pausa | `paused() == true` o evento `Paused` | 🛑 |
| Cambio de rol | evento `RoleGranted`/`RoleRevoked` o diff de holders | 🚨 crítico |
| Heartbeat | cada `HEARTBEAT_HOURS` (def. 24h), confirma que sigue vivo | 💙 |

La detección de cambios de rol funciona de dos formas en paralelo: por **eventos**
(tiempo real) y por **snapshot de holders** en cada ciclo (cubre el hueco si el
monitor estuvo caído justo cuando ocurrió el cambio).

> El script lee Guardian / Operator / Oracle-timelock / Default-admin **directo de
> la cadena**, así que no hay que hardcodear esas direcciones.

---

## 1. Crear el bot de Telegram

1. Hablá con **@BotFather** → `/newbot` → te da el `TELEGRAM_BOT_TOKEN`.
2. Mandale un mensaje cualquiera a tu bot nuevo.
3. Para tu `chat_id`: escribile a **@userinfobot**, o abrí
   `https://api.telegram.org/bot<TOKEN>/getUpdates` y buscá `"chat":{"id":...}`.

## 2. Configurar

```bash
cp .env.example .env   # y completá TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
```

## 3. Probar local

```bash
pip install -r requirements.txt
set -a && source .env && set +a
python altura_monitor.py
```

Deberías recibir "🟢 Altura monitor iniciado".

---

## 4. Desplegar en la nube

### Opción A — Docker en cualquier VPS (recomendado)

```bash
docker build -t altura-monitor .
docker run -d --name altura --restart=unless-stopped \
  --env-file .env \
  -v altura_data:/data \
  altura-monitor
docker logs -f altura
```

El volumen `altura_data` mantiene el estado entre reinicios.

### Opción B — systemd en un VPS

```ini
# /etc/systemd/system/altura-monitor.service
[Unit]
Description=Altura Vault Monitor
After=network-online.target

[Service]
WorkingDirectory=/opt/altura
EnvironmentFile=/opt/altura/.env
ExecStart=/usr/bin/python3 /opt/altura/altura_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now altura-monitor
journalctl -u altura-monitor -f
```

### Opción C — Railway / Render / Fly.io

Subí el repo, definí las variables de entorno del `.env`, comando de arranque
`python altura_monitor.py`. En Fly/Render conviene un disco persistente montado en
`/data` con `STATE_FILE=/data/altura_monitor_state.json`.

---

## Notas

- **RPC**: el público `rpc.hyperliquid.xyz/evm` sirve, pero para 24/7 conviene un
  RPC dedicado (mejor rate-limit y uptime). Cambiá `RPC_URL`.
- **No reemplaza la salida manual**: ante una alerta crítica, el script solo avisa;
  la retirada estándar tarda hasta 72 h y la instantánea depende de liquidez.
- **Riesgo no monitoreable**: la pata RWA (oro) usa un custodio off-chain ("Inessa")
  que ningún script puede auditar. Esa exposición no la cubre este monitor.
- **Ajustá umbrales**: si el PPS de Altura tiene micro-ruido, subí `PPS_DROP_BPS`
  para evitar falsos positivos; bajalo si querés máxima sensibilidad.
