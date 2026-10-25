# BCV Autopilot

Pronóstico autónomo de la tasa oficial **USD/BCV** (Banco Central de Venezuela) con
dashboard estático. Corre solo, detecta cuándo el BCV publica la nueva tasa, se
puntúa a sí mismo y se recalibra con cada dato nuevo.

> Proyecto independiente, sin afiliación con el BCV. Usa únicamente información
> pública publicada por el propio Banco Central.

---

## Qué hace

- **Vigila** la portada del BCV y detecta el cambio de *Fecha Valor* (no asume una
  hora fija: la publicación varía entre ~14:30 y ~22:00).
- **Pronostica** el USD oficial a **1 día**, **5 días** y **21 días** hábiles, con
  bandas de confianza calibradas.
- **Se corrige solo**: mantiene un pool de 7 modelos con pesos adaptativos, recalibra
  el damping y las bandas con cada observación nueva.
- **Puntúa** cada pronóstico cuando la tasa real se publica y guarda el error.
- **Publica** el estado en JSON, listo para un dashboard estático.
- **No acumula archivos**: reemplaza estado y series de forma atómica.

---

## Arquitectura

```
        BCV (portada + Excel SMC)  ──┐
        BDV JSON (fallback)        ──┤
        calendario bancario        ──┘
                                     │
                              autopilot.py
                                     │
                     ┌───────────────┴───────────────┐
                     ▼                               ▼
              state/*.json                    Cloudflare R2 (opcional)
          (estado local, siempre)                       │
                                                        ▼
                                              index.html (GitHub Pages)
```

El autopilot funciona **sin** R2: guarda todo en `state/`. R2 es solo la capa de
sincronización para que el dashboard pueda leer los datos desde cualquier lugar.

---

## Inicio rápido

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # opcional: completar credenciales de R2

python autopilot.py once    # primera pasada (siembra la serie y pronostica)
python autopilot.py status  # ver estado y pronóstico
python autopilot.py run     # loop autónomo
```

---

## Comandos

| Comando | Qué hace |
|---|---|
| `python autopilot.py run` | Loop autónomo en primer plano |
| `python autopilot.py once` | Una sola pasada (para cron o systemd timer) |
| `python autopilot.py status` | Estado, pronóstico y aciertos (no frena nada) |
| `python autopilot.py status --json` | Lo mismo en JSON |
| `python autopilot.py backtest` | Validación histórica del modelo |
| `python autopilot.py reset --yes` | Borra `state/` (la próxima corrida re-siembra) |

`status` y `backtest` funcionan incluso sin `boto3` instalado.

---

## Cómo pronostica

1. **Pool de 7 modelos**: EWMA-3, EWMA-5, drift-3, drift-10, AR(1), “medio paso” y
   naive. Cada uno propone el retorno del próximo día hábil.
2. **Pesos adaptativos**: inverse-MSE sobre los últimos 60 errores de cada modelo,
   con piso del 2 % para que ninguno muera. El que viene acertando pesa más.
3. **Damping por horizonte** (`phi`): evita extrapolar linealmente el impulso actual
   durante una semana o un mes.
4. **Anclaje histórico**: para h ≥ 2 el pronóstico mezcla 75 % impulso actual +
   25 % mediana histórica de los retornos a h días.
5. **Bandas empíricas**: cuantiles de `real / pronosticado` fuera de muestra. Se
   ensanchan con el horizonte y son asimétricas (el mercado tiene cola al alza).
6. **Detección de régimen**: compara volatilidad de 20 días contra 250 días.

### Backtest (555 días hábiles, jul-2024 → oct-2026)

| Horizonte | MAE autopilot | MAE naive | Mejora |
|---|---|---|---|
| 1 día | **0,667 Bs** (0,24 %) | 1,774 Bs | **62,4 %** |
| 5 días | **4,30 Bs** | 13,42 Bs | **68,0 %** |
| 21 días | **21,38 Bs** | 58,02 Bs | **63,2 %** |

Acierta mejor que “no cambiar nada” en el **85 %** de los días.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

La suite corre en GitHub Actions (`.github/workflows/tests.yml`). Cubre la
separación entre tasa vigente y próxima, el salto de feriados y las fechas
objetivo del pronóstico.

## Fuentes de datos

| Fuente | Uso |
|---|---|
| [Portada del BCV](https://www.bcv.org.ve/) | Valor oficial del día + *Fecha Valor* + 5 monedas |
| Excel del SMC (`tipo-cambio-de-referencia-smc`) | Semilla histórica: 555 días hábiles con BID/ASK y 21 monedas |
| [JSON del Banco de Venezuela](https://www.bancodevenezuela.com/files/tasas/tasas2.json) | Fallback / verificación (espeja la tasa BCV) |
| [Calendario bancario](https://www.bcv.org.ve/bcv/calendario-bancario) | Feriados en `data/holidays.txt` |

**No se usa mercado paralelo.** El modelo trabaja solo con la tasa oficial.

---

## Dashboard

`index.html` es un dashboard estático (Tailwind + Chart.js) que lee `forecast.json`
y `series.csv`. Muestra la **tasa vigente hoy**, la **próxima tasa** (con su Fecha
Valor), los pronósticos 1d/5d/21d con su fecha objetivo, bandas, cruces, pesos del
modelo y métricas de acierto.

### URL de R2 sin publicarla en el repo

La URL del bucket **no** se versiona. El dashboard la resuelve en este orden:

1. `window.BCV_CONFIG.R2_BASE_URL` desde `config.js` (generado desde `.env`).
2. Parámetro `?r2=https://...` en la URL (se guarda en `localStorage`).
3. `localStorage` del navegador.
4. Si no hay nada, muestra una barra para pegarla a mano.

`.gitignore` excluye `config.js`.

### GitHub Pages

1. Subí el repo a GitHub.
2. **Settings → Pages → Source: GitHub Actions**.
3. **Settings → Secrets and variables → Actions → New repository secret**:
   `R2_PUBLIC_URL` con la URL pública del bucket.
4. Push a `main`: el workflow `.github/workflows/pages.yml` copia `index.html` y
   genera `config.js` desde el secreto, sin exponer la URL en el código.

### Sin R2 (solo local)

Serví la carpeta `state/` con cualquier servidor estático y apuntá el dashboard a
esa URL:

```bash
python3 -m http.server 8080
# abrir http://localhost:8080/index.html?r2=http://localhost:8080/state
```

---

## Variables de entorno

Todas son **opcionales**. Sin ellas el autopilot corre 100 % local.

| Variable | Descripción |
|---|---|
| `R2_ACCESS_KEY_ID` | Access Key de Cloudflare R2 |
| `R2_SECRET_ACCESS_KEY` | Secret Key de Cloudflare R2 |
| `R2_ENDPOINT_URL` | Endpoint S3 de R2 (`https://<account>.r2.cloudflarestorage.com`) |
| `R2_BUCKET_NAME` | Nombre del bucket (default: `bcv-data`) |
| `R2_PUBLIC_URL` | URL pública del bucket, usada para generar `config.js` |

---

## Antes de publicar

`.env`, `state/`, `.venv/`, `config.js` y `__pycache__/` están en `.gitignore`.
Verificá con `git status` que no aparezcan antes del primer push.

## Estructura del repo

```
.
├── autopilot.py              # motor autónomo
├── index.html                # dashboard
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── AUTOPILOT.md              # guía detallada de operación
├── .github/workflows/pages.yml
├── data/
│   ├── bcv_smc_daily.csv     # semilla histórica (555 días)
│   └── holidays.txt          # feriados bancarios (editable)
└── state/                    # runtime, ignorado por git
    ├── series.csv
    ├── state.json
    ├── forecast.json
    ├── metrics.json
    ├── forecasts_log.csv
    └── events.log
```

---

## Despliegue

### systemd (usuario)

```ini
# ~/.config/systemd/user/bcv-autopilot.service
[Unit]
Description=BCV Autopilot
After=network-online.target

[Service]
WorkingDirectory=%h/bcv-predictor
ExecStart=%h/bcv-predictor/.venv/bin/python autopilot.py run
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now bcv-autopilot
journalctl --user -u bcv-autopilot -f
```

### cron

```cron
*/30 * * * * cd /ruta/bcv-predictor && .venv/bin/python autopilot.py once >> state/cron.log 2>&1
```

El autopilot usa un candado (`state/autopilot.pid`) para evitar dos instancias.

---

## Limitaciones

- **Los saltos no se predicen.** El modelo acierta el paso normal; un ajuste de
  +0,5 % a +3 % se sale de cualquier banda.
- **El mes es un escenario**, no una promesa: la banda del 80 % es amplia a propósito.
- **Los cruces** (EUR/CNY/TRY/RUB) dependen del mercado internacional y se proyectan
  con la cotización del día; sirven de referencia.
- **Feriados**: se leen de `data/holidays.txt`; hay que actualizarlos cada año.
- Si el BCV cambia la metodología (nuevo convenio, banda, intervención), hay que
  revisar el modelo.

---

## Aviso

Datos públicos, sin garantía de exactitud. No es asesoría financiera. El BCV es la
única fuente oficial de la tasa de referencia.
