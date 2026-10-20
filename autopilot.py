#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BCV AUTOPILOT — pronóstico autónomo de la tasa USD del BCV.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# --- Dependencias opcionales (R2 / .env) ---
# El autopilot funciona igual sin ellas: cae a almacenamiento local.
try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    boto3 = None

    class ClientError(Exception):
        """Placeholder cuando botocore no está instalado."""

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

# Cargar .env junto al script (independiente del directorio de trabajo)
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if load_dotenv:
    load_dotenv(_ENV_FILE)

# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, "state")
SERIES_CSV = os.path.join(STATE_DIR, "series.csv")
STATE_JSON = os.path.join(STATE_DIR, "state.json")
FORECAST_JSON = os.path.join(STATE_DIR, "forecast.json")
METRICS_JSON = os.path.join(STATE_DIR, "metrics.json")
FLOG_CSV = os.path.join(STATE_DIR, "forecasts_log.csv")
EVENTS_LOG = os.path.join(STATE_DIR, "events.log")
PID_FILE = os.path.join(STATE_DIR, "autopilot.pid")

DATA_DIR = os.path.join(BASE, "data")
SEED_CSV = os.path.join(DATA_DIR, "bcv_smc_daily.csv")
HOLIDAYS_TXT = os.path.join(DATA_DIR, "holidays.txt")

URL = "https://www.bcv.org.ve/"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

try:
    TZ = ZoneInfo("America/Caracas")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=-4), "VET")

IDS = {"USD": "dolar", "EUR": "euro", "CNY": "yuan", "TRY": "lira", "RUB": "rublo"}
MODELS = ["ewma3", "ewma5", "drift3", "drift10", "ar1", "half", "naive"]
HORIZONS = (1, 5, 21)

POLL_FAST = int(os.environ.get("BCV_POLL_FAST", "180"))    # ventana de publicación
POLL_SLOW = int(os.environ.get("BCV_POLL_SLOW", "1800"))   # resto del día
FAST_FROM = 13 * 60      # 13:00
FAST_TO = 21 * 60        # 21:00

MAX_LOG = 3000
MAX_EVENTS = 500
MAX_MODEL_ERRORS = 200

# ----------------------------------------------------------------------------
# Cloudflare R2 (opcional)
# ----------------------------------------------------------------------------
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT_URL")
R2_BUCKET = os.environ.get("R2_BUCKET_NAME", "bcv-data")
R2_PUBLIC_URL = (os.environ.get("R2_PUBLIC_URL") or "").rstrip("/")

R2_FILES = ["state.json", "series.csv", "metrics.json", "forecast.json", "forecasts_log.csv"]
R2_CONTENT_TYPES = {
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
}
R2_HEARTBEAT = 1800  # segundos entre subidas sin cambios

_r2_client = None


def get_r2_client():
    """Cliente R2 cacheado. None si falta boto3 o la configuracion."""
    global _r2_client
    if boto3 is None:
        return None
    if not all([R2_ACCESS_KEY, R2_SECRET_KEY, R2_ENDPOINT]):
        return None
    if _r2_client is None:
        _r2_client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name="auto",
        )
    return _r2_client


def r2_disponible() -> bool:
    return get_r2_client() is not None


def _leer_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _stamp_path() -> str:
    return os.path.join(STATE_DIR, ".last_upload")


def _ultima_subida() -> float:
    try:
        with open(_stamp_path(), encoding="utf-8") as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        return 0.0


def descargar_de_r2(verbose: bool = False) -> bool:
    """Baja el estado de R2 solo si es mas nuevo que el local (atomico)."""
    client = get_r2_client()
    if not client:
        if verbose:
            print("R2 no configurado; se usa el estado local.")
        return False

    tmp_state = STATE_JSON + ".tmp"
    try:
        client.download_file(R2_BUCKET, "state.json", tmp_state)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey"):
            if verbose:
                print("R2 vacio (primera ejecucion).")
        elif verbose:
            print(f"No se pudo bajar state.json: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"Error de red bajando state.json: {exc}")
        return False

    remoto = _leer_json(tmp_state)
    local = _leer_json(STATE_JSON) or {}
    if not remoto:
        try:
            os.remove(tmp_state)
        except OSError:
            pass
        return False

    clave_remota = (str(remoto.get("last_date") or ""), str(remoto.get("last_poll") or ""))
    clave_local = (str(local.get("last_date") or ""), str(local.get("last_poll") or ""))
    if clave_remota <= clave_local:
        try:
            os.remove(tmp_state)
        except OSError:
            pass
        if verbose:
            print("R2 no es mas nuevo que el estado local; no se descarga.")
        return False

    os.replace(tmp_state, STATE_JSON)
    for nombre in R2_FILES:
        if nombre == "state.json":
            continue
        final = os.path.join(STATE_DIR, nombre)
        tmp = final + ".tmp"
        try:
            client.download_file(R2_BUCKET, nombre, tmp)
            os.replace(tmp, final)
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"No se pudo bajar {nombre}: {exc}")
            try:
                os.remove(tmp)
            except OSError:
                pass
    if verbose:
        print(f"Estado actualizado desde R2 (Fecha Valor {clave_remota[0]}).")
    return True


def subir_a_r2(forzar: bool = False, verbose: bool = False) -> bool:
    """Sube el estado a R2. Sin cambios no sube, salvo heartbeat cada 30 min."""
    client = get_r2_client()
    if not client:
        return False
    ahora = time.time()
    if not forzar and (ahora - _ultima_subida()) < R2_HEARTBEAT:
        return False

    subidos = 0
    for nombre in R2_FILES:
        local_path = os.path.join(STATE_DIR, nombre)
        if not os.path.exists(local_path):
            continue
        ext = os.path.splitext(nombre)[1].lower()
        try:
            client.upload_file(
                local_path,
                R2_BUCKET,
                nombre,
                ExtraArgs={
                    "ContentType": R2_CONTENT_TYPES.get(ext, "application/octet-stream"),
                    "CacheControl": "no-cache",
                },
            )
            subidos += 1
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"Error subiendo {nombre}: {exc}")
    if subidos:
        try:
            atomic_write(_stamp_path(), f"{ahora:.0f}")
        except OSError:
            pass
        if verbose:
            print(f"{subidos} archivo(s) subidos a R2.")
    return subidos > 0


def escribir_config_js():
    """Genera config.js para el dashboard a partir de R2_PUBLIC_URL."""
    if not R2_PUBLIC_URL:
        return
    destino = os.path.join(BASE, "config.js")
    contenido = f'window.BCV_CONFIG = {{ R2_BASE_URL: "{R2_PUBLIC_URL}" }};\n'
    try:
        with open(destino, encoding="utf-8") as fh:
            if fh.read() == contenido:
                return
    except OSError:
        pass
    atomic_write(destino, contenido)


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def ensure_dirs():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)


def now_local() -> datetime:
    return datetime.now(TZ)


def atomic_write(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def append_event(msg: str):
    line = f"{now_local():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        lines = []
        if os.path.exists(EVENTS_LOG):
            with open(EVENTS_LOG, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        lines.append(line)
        lines = lines[-MAX_EVENTS:]
        atomic_write(EVENTS_LOG, "\n".join(lines) + "\n")
    except Exception:
        pass


def fmt(x, dec=4):
    """Formato: 1.234,5678"""
    if x is None:
        return "-"
    s = f"{x:,.{dec}f}"
    return s.replace(",", "\u00a7").replace(".", ",").replace("\u00a7", ".")


# ----------------------------------------------------------------------------
# Red
# ----------------------------------------------------------------------------
def http_get(url: str, timeout: int = 40) -> str:
    try:
        res = subprocess.run(
            ["curl", "-sSk", "-L", "--max-time", str(timeout), "-A", UA, url],
            capture_output=True,
            timeout=timeout + 15,
        )
        if res.returncode == 0 and res.stdout:
            return res.stdout.decode("utf-8", "replace")
    except Exception:
        pass
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_num(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def parse_home(html: str) -> dict:
    out = {}
    for code, cid in IDS.items():
        m = re.search(
            r'<div\s+id="%s".*?<strong[^>]*>\s*([0-9][0-9.,]*)\s*</strong>' % cid,
            html,
            re.S | re.I,
        )
        if not m:
            raise RuntimeError(f"no se encontró {code} en la portada")
        out[code] = parse_num(m.group(1))

    m = re.search(r"Fecha Valor:.*?content=\"(\d{4}-\d{2}-\d{2})T", html, re.S)
    if not m:
        raise RuntimeError("no se encontró la Fecha Valor")
    out["date"] = datetime.strptime(m.group(1), "%Y-%m-%d").date()
    return out


def fetch_home() -> dict:
    last_err = None
    for attempt in range(1, 4):
        try:
            home = parse_home(http_get(URL))
            # validación anti-basura
            if home["USD"] <= 0:
                raise RuntimeError("USD inválido")
            return home
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < 3:
                time.sleep(2)
    raise RuntimeError(f"BCV inaccesible: {last_err}")


def fetch_bdv() -> dict | None:
    """Fallback: JSON del Banco de Venezuela (espejo exacto de la tasa BCV)."""
    try:
        raw = http_get("https://www.bancodevenezuela.com/files/tasas/tasas2.json", timeout=20)
        data = json.loads(raw)
        usd = parse_num(data["mesacambio"]["bcv"]["dolares"])
        eur = parse_num(data["mesacambio"]["bcv"]["euros"])
        out = {"USD": usd, "EUR": eur}
        for code in ("CNY", "TRY", "RUB"):
            out[code] = None
        return out
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Calendario y fechas
# ----------------------------------------------------------------------------
def load_holidays() -> set[date]:
    hol = set()
    if not os.path.exists(HOLIDAYS_TXT):
        return hol
    with open(HOLIDAYS_TXT, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if line:
                try:
                    hol.add(datetime.strptime(line, "%Y-%m-%d").date())
                except ValueError:
                    pass
    return hol


def is_bd(d: date, hol: set[date]) -> bool:
    return d.weekday() < 5 and d not in hol


def add_bd(d: date, n: int, hol: set[date]) -> date:
    while n > 0:
        d += timedelta(days=1)
        if is_bd(d, hol):
            n -= 1
    return d


# ----------------------------------------------------------------------------
# Serie
# ----------------------------------------------------------------------------
def load_series() -> list[dict]:
    if not os.path.exists(SERIES_CSV):
        return []
    rows = []
    with open(SERIES_CSV, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                rows.append(
                    {
                        "date": datetime.strptime(row["fecha_valor"], "%Y-%m-%d").date(),
                        "usd": float(row["usd_ask"]),
                        "bid": float(row["usd_bid"]) if row.get("usd_bid") else None,
                        "eur": float(row["eur"]) if row.get("eur") else None,
                        "cny": float(row["cny"]) if row.get("cny") else None,
                        "try": float(row["try"]) if row.get("try") else None,
                        "rub": float(row["rub"]) if row.get("rub") else None,
                        "src": row.get("src", "seed"),
                    }
                )
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda r: r["date"])
    return rows


def save_series(rows: list[dict]):
    out = ["fecha_valor,usd_ask,usd_bid,eur,cny,try,rub,src"]
    for r in rows:
        out.append(
            ",".join(
                [
                    r["date"].isoformat(),
                    f"{r['usd']:.8f}",
                    f"{r['bid']:.8f}" if r.get("bid") else "",
                    f"{r['eur']:.8f}" if r.get("eur") else "",
                    f"{r['cny']:.8f}" if r.get("cny") else "",
                    f"{r['try']:.8f}" if r.get("try") else "",
                    f"{r['rub']:.8f}" if r.get("rub") else "",
                    r.get("src", "live"),
                ]
            )
        )
    atomic_write(SERIES_CSV, "\n".join(out) + "\n")


def seed_from_csv() -> list[dict]:
    rows = []
    if not os.path.exists(SEED_CSV):
        return rows
    with open(SEED_CSV, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                rows.append(
                    {
                        "date": datetime.strptime(row["fecha_valor"], "%Y-%m-%d").date(),
                        "usd": float(row["USD_ask_bs"]),
                        "bid": float(row["USD_bid_bs"]),
                        "eur": float(row["EUR_ask_bs"]),
                        "cny": float(row["CNY_ask_bs"]),
                        "try": float(row["TRY_ask_bs"]),
                        "rub": float(row["RUB_ask_bs"]),
                        "src": "seed",
                    }
                )
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda r: r["date"])
    return rows


def row_from_home(home: dict, src="live") -> dict:
    usd = home["USD"]
    return {
        "date": home["date"],
        "usd": usd,
        "bid": usd * 0.9975,
        "eur": home.get("EUR"),
        "cny": home.get("CNY"),
        "try": home.get("TRY"),
        "rub": home.get("RUB"),
        "src": src,
    }


# ----------------------------------------------------------------------------
# Matemática del modelo
# ----------------------------------------------------------------------------
def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


ALPHA = 0.75          # peso del impulso actual vs. mediana historica (h>=2)
MED_WINDOW = 120      # ventana para la mediana historica del horizonte


def horizon_median(values: list[float], upto: int, h: int, win: int = MED_WINDOW) -> float:
    """Mediana de los retornos log a h dias observados hasta `upto`."""
    if upto - h < 20:
        return 0.0
    start = max(20, upto - win - h)
    vals = [
        math.log(values[j + h] / values[j])
        for j in range(start, upto - h + 1)
        if j + h <= upto
    ]
    return median(vals)


def log_returns(values: list[float]) -> list[float]:
    return [math.log(values[i] / values[i - 1]) for i in range(1, len(values))]


def ewma_last(xs: list[float], span: int) -> float:
    alpha = 2.0 / (span + 1.0)
    v = float(xs[0])
    for x in xs[1:]:
        v = alpha * float(x) + (1 - alpha) * v
    return v


def ar1_pred(r: list[float]) -> float:
    n = min(120, len(r) - 1)
    if n < 10:
        return mean(r[-3:]) if r else 0.0
    y = r[-n:]
    x = r[-n - 1: -1]
    mx, my = mean(x), mean(y)
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    var = sum((a - mx) ** 2 for a in x) + 1e-12
    beta = max(-0.2, min(0.95, cov / var))
    return my + beta * (r[-1] - mx)


def model_preds(r: list[float]) -> dict:
    if not r:
        return {m: 0.0 for m in MODELS}
    return {
        "ewma3": ewma_last(r[-15:], 3),
        "ewma5": ewma_last(r[-25:], 5),
        "drift3": mean(r[-3:]),
        "drift10": mean(r[-10:]),
        "ar1": ar1_pred(r),
        "half": 0.5 * r[-1],
        "naive": 0.0,
    }


def compute_weights(model_errors: dict) -> dict:
    inv = {}
    for m in MODELS:
        errs = model_errors.get(m, [])[-60:]
        mse = mean(errs) if errs else 1e-8
        inv[m] = 1.0 / (mse + 1e-10)
    total = sum(inv.values())
    w = {m: inv[m] / total for m in MODELS}
    floor = 0.02
    w = {m: max(v, floor) for m, v in w.items()}
    total = sum(w.values())
    return {m: v / total for m, v in w.items()}


def combined_rh(preds: dict, weights: dict) -> float:
    return sum(weights.get(m, 0.0) * preds.get(m, 0.0) for m in MODELS)


def damp_sum(phi: float, h: int) -> float:
    return sum(phi ** j for j in range(h))


def calibrate_phi(values: list[float], h: int) -> float:
    if len(values) < 60:
        return 1.0 if h <= 5 else 0.9
    r = log_returns(values)
    n = len(values)
    start = max(20, n - 300)
    best_phi, best_mse = 1.0, float("inf")
    for k in range(50, 101, 2):
        phi = k / 100.0
        se, cnt = 0.0, 0
        for t in range(start, n - h):
            rh = ewma_last(r[max(0, t - 15): t], 3)
            pred = values[t] * math.exp(rh * damp_sum(phi, h))
            se += (math.log(values[t + h] / pred)) ** 2
            cnt += 1
        if cnt and se / cnt < best_mse:
            best_mse, best_phi = se / cnt, phi
    return best_phi


def quantile(vals: list[float], q: float) -> float:
    if not vals:
        return 1.0
    s = sorted(vals)
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def calibrate_intervals(values: list[float], h: int, phi: float) -> dict:
    r = log_returns(values)
    n = len(values)
    start = max(20, n - 300)
    ratios = []
    for t in range(start, n - h):
        rh = ewma_last(r[max(0, t - 15): t], 3)
        med = horizon_median(values, t, h)
        pred = point_forecast(values[t], rh, h, phi, med)
        ratios.append(values[t + h] / pred)
    if len(ratios) < 10:
        return {"p5": 0.99, "p25": 0.997, "p75": 1.003, "p95": 1.01, "n": len(ratios)}
    return {
        "p5": quantile(ratios, 0.05),
        "p25": quantile(ratios, 0.25),
        "p75": quantile(ratios, 0.75),
        "p95": quantile(ratios, 0.95),
        "n": len(ratios),
    }


def calibrate_all(values: list[float]):
    phis = {"1": 1.0, "5": calibrate_phi(values, 5), "21": calibrate_phi(values, 21)}
    intervals = {}
    for h in range(1, 22):
        if h <= 5:
            phi = phis["5"] if h > 1 else 1.0
        else:
            phi = phis["5"] + (phis["21"] - phis["5"]) * (h - 5) / 16.0
        intervals[str(h)] = calibrate_intervals(values, h, phi)
    return phis, intervals


def point_forecast(last: float, rh: float, h: int, phi: float, med: float = 0.0) -> float:
    if h <= 1:
        return last * math.exp(rh)
    return last * math.exp(ALPHA * rh * damp_sum(phi, h) + (1.0 - ALPHA) * med)


def phi_for(h: int, phis: dict) -> float:
    if h <= 1:
        return 1.0
    if h <= 5:
        return phis.get("5", 0.95)
    return phis.get("5", 0.95) + (phis.get("21", 0.9) - phis.get("5", 0.95)) * (h - 5) / 16.0


# ----------------------------------------------------------------------------
# Estado y pronósticos
# ----------------------------------------------------------------------------
def new_state() -> dict:
    return {
        "version": 1,
        "created": now_local().isoformat(timespec="seconds"),
        "initialized": False,
        "last_poll": None,
        "last_event": None,
        "weights": {m: 1.0 / len(MODELS) for m in MODELS},
        "model_errors": {},
        "model_preds_next": {},
        "phis": {"1": 1.0, "5": 0.95, "21": 0.9},
        "intervals": {},
        "pending": [],
        "log": [],
        "pub_times": [],
        "cross_ratios": {},
        "last_forecast_at": None,
        "last_data_at": None,
        "consecutive_errors": 0,
        "last_error": None,
        "next_target": None,
        "last_value": None,
        "last_date": None,
    }


def score_pending(state: dict, obs_date: date, obs_value: float) -> list[dict]:
    """Puntúa los pronósticos cuyo objetivo acaba de publicarse."""
    scored = []
    keep = []
    for p in state.get("pending", []):
        try:
            target = datetime.strptime(p["target"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if target < obs_date:
            # objetivo que nunca se publico (feriado no listado o dia sin tasa)
            continue
        if target != obs_date:
            keep.append(p)
            continue
        err = p["pred"] - obs_value
        pct = err / obs_value * 100.0
        row = {
            "target": p["target"],
            "h": p["h"],
            "issued_at": p.get("issued_at"),
            "pred": p["pred"],
            "lo80": p.get("lo"),
            "hi80": p.get("hi"),
            "actual": obs_value,
            "err_bs": err,
            "err_pct": pct,
            "in80": bool(p.get("lo") is not None and p["lo"] <= obs_value <= p["hi"]),
        }
        state.setdefault("log", []).append(row)
        scored.append(row)
    state["pending"] = keep
    if len(state.get("log", [])) > MAX_LOG:
        state["log"] = state["log"][-MAX_LOG:]
    return scored


def issue_official(state: dict, values: list[float], last_date: date, last_value: float,
                   rh: float, phis: dict, intervals: dict, hol: set[date], issued_at: str):
    existing = {(p["target"], p["h"]) for p in state.get("pending", [])}
    for h in HORIZONS:
        target = add_bd(last_date, h, hol).isoformat()
        if (target, h) in existing:
            continue
        phi = phi_for(h, phis)
        med = horizon_median(values, len(values) - 1, h)
        pred = point_forecast(last_value, rh, h, phi, med)
        iv = intervals.get(str(h), {"p5": 0.99, "p95": 1.01})
        state.setdefault("pending", []).append(
            {
                "target": target,
                "h": h,
                "issued_at": issued_at,
                "base_date": last_date.isoformat(),
                "base_value": last_value,
                "pred": pred,
                "lo": pred * iv["p5"],
                "hi": pred * iv["p95"],
            }
        )
    # limpieza de pendientes viejos
    cutoff = last_date - timedelta(days=90)
    state["pending"] = [
        p
        for p in state.get("pending", [])
        if datetime.strptime(p["target"], "%Y-%m-%d").date() >= cutoff
    ]


def build_path(values: list[float], last_date: date, last_value: float, rh: float,
               phis: dict, intervals: dict, hol: set[date], n: int = 21) -> list[dict]:
    path = []
    for h in range(1, n + 1):
        phi = phi_for(h, phis)
        med = horizon_median(values, len(values) - 1, h)
        pred = point_forecast(last_value, rh, h, phi, med)
        iv = intervals.get(str(h), {"p5": 0.99, "p25": 0.997, "p75": 1.003, "p95": 1.01})
        target = add_bd(last_date, h, hol)
        path.append(
            {
                "h": h,
                "date": target.isoformat(),
                "pred": pred,
                "lo50": pred * iv["p25"],
                "hi50": pred * iv["p75"],
                "lo80": pred * iv["p5"],
                "hi80": pred * iv["p95"],
            }
        )
    return path


def metrics_from_log(log: list[dict]) -> dict:
    out = {}
    for h in HORIZONS:
        rows = [r for r in log if r.get("h") == h]
        cut = rows[-50:]
        if not cut:
            out[str(h)] = {"n": 0}
            continue
        errs = [abs(r["err_bs"]) for r in cut]
        pcts = [abs(r["err_pct"]) for r in cut]
        out[str(h)] = {
            "n": len(cut),
            "mae_bs": mean(errs),
            "mape_pct": mean(pcts),
            "bias_bs": mean([r["err_bs"] for r in cut]),
            "hit80_pct": 100.0 * sum(1 for r in cut if r["in80"]) / len(cut),
            "last": cut[-1],
        }
    return out


def save_forecast_log(state: dict):
    """Escribe la bitacora de pronosticos puntuados (reemplazo completo)."""
    log = state.get("log", [])
    header = ["target", "h", "issued_at", "pred", "lo80", "hi80", "actual", "err_bs", "err_pct", "in80"]
    lines = [",".join(header)]
    for row in log[-MAX_LOG:]:
        lines.append(",".join(str(row.get(k, "")) for k in header))
    atomic_write(FLOG_CSV, "\n".join(lines) + "\n")


def compute_regime(values: list[float]) -> dict:
    r = log_returns(values)
    if len(r) < 30:
        return {"vol20": None, "vol250": None, "ratio": None}
    vol20 = (sum((x - mean(r[-20:])) ** 2 for x in r[-20:]) / 19) ** 0.5
    vol250 = (sum((x - mean(r[-250:])) ** 2 for x in r[-250:]) / max(1, len(r[-250:]) - 1)) ** 0.5
    return {
        "vol20_pct": vol20 * 100,
        "vol250_pct": vol250 * 100,
        "ratio": (vol20 / vol250) if vol250 else None,
    }


# ----------------------------------------------------------------------------
# Ciclo principal
# ----------------------------------------------------------------------------
def write_forecast_file(state: dict, rows: list[dict], hol: set[date]):
    if not rows:
        return
    last = rows[-1]
    values = [r["usd"] for r in rows]
    r = log_returns(values)
    preds = model_preds(r)
    rh = combined_rh(preds, state["weights"])
    path = build_path(values, last["date"], last["usd"], rh, state["phis"], state["intervals"], hol, 21)

    official = {}
    for h in HORIZONS:
        p = next((x for x in path if x["h"] == h), None)
        if p:
            official[str(h)] = p

    crosses = {}
    ratios = state.get("cross_ratios", {})
    for h in HORIZONS:
        p = official.get(str(h))
        if not p:
            continue
        crosses[str(h)] = {
            c: p["pred"] * ratios[c] for c in ("EUR", "CNY", "TRY", "RUB") if c in ratios
        }

    forecast = {
        "generated": now_local().isoformat(timespec="seconds"),
        "data_updated": state.get("last_data_at"),
        "last": {"date": last["date"].isoformat(), "usd": last["usd"]},
        "next_target": add_bd(last["date"], 1, hol).isoformat(),
        "official": official,
        "path": path,
        "crosses": crosses,
        "weights": state["weights"],
        "phis": state["phis"],
        "rh": rh,
        "regime": compute_regime(values),
        "metrics": metrics_from_log(state.get("log", [])),
        "health": {
            "consecutive_errors": state.get("consecutive_errors", 0),
            "last_error": state.get("last_error"),
        },
    }
    save_json(FORECAST_JSON, forecast)
    return forecast


def warm_start(values: list[float]):
    """Errores in-sample del ultimo tramo para arrancar con pesos informados."""
    r = log_returns(values)
    errors = {m: [] for m in MODELS}
    start = max(60, len(values) - 250)
    for t in range(start, len(values) - 1):
        preds = model_preds(r[:t])
        ract = r[t]
        for m in MODELS:
            errors[m].append((preds[m] - ract) ** 2)
    errors = {m: errors[m][-MAX_MODEL_ERRORS:] for m in MODELS}
    return compute_weights(errors), errors


def on_new_observation(state: dict, rows: list[dict], hol: set[date], verbose=False):
    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    values = [r["usd"] for r in rows]
    was_init = not state.get("initialized")
    now_iso = now_local().isoformat(timespec="seconds")

    # 1) puntuar pendientes con esta fecha
    scored = score_pending(state, last["date"], last["usd"])

    # 2) errores de cada modelo con el retorno observado
    if prev is not None and state.get("model_preds_next"):
        r_actual = math.log(last["usd"] / prev["usd"])
        for m, p in state["model_preds_next"].items():
            e = (p - r_actual) ** 2
            lst = state.setdefault("model_errors", {}).setdefault(m, [])
            lst.append(e)
            del lst[:-MAX_MODEL_ERRORS]
        state["weights"] = compute_weights(state["model_errors"])

    if not state.get("model_errors"):
        state["weights"], state["model_errors"] = warm_start(values)

    # 3) calibrar damping y bandas
    state["phis"], state["intervals"] = calibrate_all(values)

    # 4) predicciones por modelo para el próximo paso
    r = log_returns(values)
    preds = model_preds(r)
    state["model_preds_next"] = preds
    rh = combined_rh(preds, state["weights"])

    # 5) lanzar pronósticos oficiales
    issue_official(state, values, last["date"], last["usd"], rh, state["phis"], state["intervals"], hol, now_iso)

    # 6) ratios de cruces
    if last.get("eur") and last["usd"]:
        state["cross_ratios"] = {
            "EUR": last["eur"] / last["usd"],
            "CNY": last["cny"] / last["usd"] if last.get("cny") else None,
            "TRY": last["try"] / last["usd"] if last.get("try") else None,
            "RUB": last["rub"] / last["usd"] if last.get("rub") else None,
        }
        state["cross_ratios"] = {k: v for k, v in state["cross_ratios"].items() if v}

    # 7) métricas y régimen
    save_json(METRICS_JSON, metrics_from_log(state.get("log", [])))
    save_forecast_log(state)
    state["last_value"] = last["usd"]
    state["last_date"] = last["date"].isoformat()
    state["last_data_at"] = now_iso
    state["next_target"] = add_bd(last["date"], 1, hol).isoformat()
    state["last_forecast_at"] = now_iso
    state["initialized"] = True

    # 8) log
    if scored:
        for s in scored:
            append_event(
                f"PUNTUADO h={s['h']} target={s['target']} pred={s['pred']:.4f} "
                f"real={s['actual']:.4f} err={s['err_pct']:+.3f}%"
            )
    msg = (
        f"{'INICIALIZADO' if was_init else 'NUEVO DATO'} {last['date']} "
        f"USD={last['usd']:.6f} (detectado {now_local():%H:%M}) rh={rh*100:.4f}%/dia"
    )
    append_event(msg)
    state["last_event"] = msg
    if verbose:
        print(msg)


def tick(verbose=False) -> bool:
    """Una pasada. Devuelve True si hubo dato nuevo."""
    ensure_dirs()
    state = load_json(STATE_JSON, None) or new_state()
    errores_previos = state.get("consecutive_errors", 0)
    rows = load_series()
    if not rows:
        rows = seed_from_csv()
        if rows:
            save_series(rows)
            append_event(f"sembrada serie histórica: {len(rows)} días")

    new = False
    changed = False
    try:
        home = fetch_home()
    except Exception as exc:  # noqa: BLE001
        append_event(f"ERROR portada BCV: {exc}")
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        state["last_error"] = str(exc)
        if verbose:
            print(f"ERROR: {exc}")
        home = None

    if home is not None:
        state["consecutive_errors"] = 0
        state["last_error"] = None

    if home:
        if not rows or home["date"] > rows[-1]["date"]:
            rows.append(row_from_home(home))
            rows.sort(key=lambda r: r["date"])
            new = True
            changed = True
        elif home["date"] == rows[-1]["date"]:
            fresh = row_from_home(home)
            if abs(fresh["usd"] - rows[-1]["usd"]) > 1e-9:
                rows[-1] = fresh
                changed = True
        else:
            pass  # página vieja / caché

    if changed:
        save_series(rows)

    inicializando = not state.get("initialized")
    if new or inicializando:
        if rows:
            on_new_observation(state, rows, load_holidays(), verbose=verbose)

    state["last_poll"] = now_local().isoformat(timespec="seconds")
    save_json(STATE_JSON, state)
    if rows:
        write_forecast_file(state, rows, load_holidays())
        
    # Sube a R2 si hubo cambio, si cambio el health, o por heartbeat cada 30 min.
    # Del health solo subimos las transiciones 0->1->2 y la recuperacion, para no
    # martillar R2 durante una caida larga del BCV.
    errores_actuales = state.get("consecutive_errors", 0)
    cambio_health = errores_actuales != errores_previos and errores_actuales <= 2
    subir_a_r2(
        forzar=(new or changed or inicializando or cambio_health),
        verbose=verbose,
    )

    return new


def poll_interval() -> int:
    t = now_local()
    minutes = t.hour * 60 + t.minute
    if FAST_FROM <= minutes <= FAST_TO:
        return POLL_FAST
    return POLL_SLOW


# ----------------------------------------------------------------------------
# Comandos
# ----------------------------------------------------------------------------
def acquire_lock() -> bool:
    ensure_dirs()
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as fh:
                pid = int(fh.read().strip())
            os.kill(pid, 0)
            print(f"Ya hay una instancia corriendo (PID {pid}).")
            return False
        except (ValueError, OSError):
            pass
    with open(PID_FILE, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def iniciar_servidor_falso():
    """Mini servidor web para que Render no apague el bot por no tener puerto HTTP."""
    class PingHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"BCV Autopilot is Alive!")
        def log_message(self, format, *args):
            pass # Silenciar logs de pings para no llenar la consola
            
    puerto = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(('0.0.0.0', puerto), PingHandler)
        print(f"[Render] Servidor de ping escuchando en el puerto {puerto}")
        server.serve_forever()
    except Exception as e:
        print(f"Error iniciando servidor falso: {e}")


def cmd_run(args):
    # Iniciar el servidor falso en un hilo separado para mantener Render despierto
    threading.Thread(target=iniciar_servidor_falso, daemon=True).start()

    if not acquire_lock():
        return 1
    stop = {"flag": False}

    def handler(signum, frame):  # noqa: ARG001
        stop["flag"] = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    print(f"BCV Autopilot corriendo (PID {os.getpid()}). Ctrl+C para salir.")
    escribir_config_js()
    descargar_de_r2(verbose=True)
    
    append_event(f"autopilot iniciado PID {os.getpid()}")
    while not stop["flag"]:
        try:
            tick(verbose=True)
        except Exception as exc:  # noqa: BLE001
            append_event(f"ERROR tick: {type(exc).__name__}: {exc}")
            print(f"ERROR: {exc}")
        for _ in range(poll_interval()):
            if stop["flag"]:
                break
            time.sleep(1)
    append_event("autopilot detenido")
    release_lock()
    print("Autopilot detenido.")
    return 0


def cmd_once(args):
    escribir_config_js()
    descargar_de_r2(verbose=True)
    new = tick(verbose=True)
    print("Dato nuevo." if new else "Sin cambios.")
    return 0


def cmd_status(args):
    state = load_json(STATE_JSON, None) or new_state()
    forecast = load_json(FORECAST_JSON, None)
    metrics = load_json(METRICS_JSON, {})
    rows = load_series()
    hol = load_holidays()

    if args.json:
        print(json.dumps({"state": state, "forecast": forecast, "metrics": metrics}, ensure_ascii=False, indent=2, default=str))
        return 0

    print("=" * 68)
    print("  BCV AUTOPILOT — ESTADO")
    print("=" * 68)
    if not rows:
        print("Sin datos todavía. Corré: python3 autopilot.py once")
        return 1

    last = rows[-1]
    print(f"Última tasa     : {fmt(last['usd'], 8)} Bs/USD   (Fecha Valor {last['date']:%d/%m/%Y})")
    print(f"Último sondeo   : {state.get('last_poll') or '-'}")
    print(f"Próximo objetivo: {state.get('next_target') or '-'}")
    pub = state.get("pub_times", [])[-10:]
    if pub:
        print(f"Publicaciones   : {', '.join(pub)}  (últimas {len(pub)})")
    reg = (forecast or {}).get("regime", {})
    if reg.get("ratio"):
        print(f"Régimen         : vol20={reg['vol20_pct']:.3f}%  vol250={reg['vol250_pct']:.3f}%  ratio={reg['ratio']:.2f}")

    if forecast:
        print()
        print("PRONÓSTICO USD (sin redondear / redondeado)")
        for h in HORIZONS:
            p = forecast.get("official", {}).get(str(h))
            if not p:
                continue
            etiqueta = "día " if h == 1 else "días"
            print(
                f"  +{h:>2} {etiqueta} ({p['date']}): {fmt(p['pred'], 6)}  ->  {fmt(p['pred'], 2)}"
                f"   [50%: {fmt(p['lo50'], 2)}–{fmt(p['hi50'], 2)} | 80%: {fmt(p['lo80'], 2)}–{fmt(p['hi80'], 2)}]"
            )
        print()
        print("CRUCES PROYECTADOS (referencia)")
        for h in HORIZONS:
            c = forecast.get("crosses", {}).get(str(h))
            if not c:
                continue
            txt = " · ".join(f"{k} {fmt(v, 4)} ({fmt(v, 2)})" for k, v in c.items())
            print(f"  +{h:>2} días: {txt}")

    print()
    print("PESOS DEL POOL")
    for m, w in sorted(state.get("weights", {}).items(), key=lambda x: -x[1]):
        print(f"  {m:<8} {w*100:5.1f}%")

    if metrics:
        print()
        print("ACIERTOS (últimos 50 por horizonte)")
        for h in HORIZONS:
            m = metrics.get(str(h), {})
            if m.get("n"):
                print(
                    f"  h={h:>2}: n={m['n']:<3} MAE={m['mae_bs']:.4f} Bs  "
                    f"MAPE={m['mape_pct']:.4f}%  sesgo={m['bias_bs']:+.4f} Bs  dentro80={m['hit80_pct']:.1f}%"
                )
    return 0


def cmd_backtest(args):
    rows = load_series() or seed_from_csv()
    if len(rows) < 120:
        print("Muy pocos datos para backtest.")
        return 1
    values = [r["usd"] for r in rows]
    r = log_returns(values)

    # --- h=1 con pool online ---
    errors = {}
    n = len(values)
    start = 80
    abs_err, pct_err, naive_err = [], [], []
    correct_naive = 0
    for t in range(start, n - 1):
        preds = model_preds(r[:t])
        w = compute_weights(errors)
        rh = combined_rh(preds, w)
        pred = values[t] * math.exp(rh)
        act = values[t + 1]
        abs_err.append(abs(pred - act))
        pct_err.append(abs(pred - act) / act * 100)
        naive_err.append(abs(values[t] - act))
        if abs(pred - act) < abs(values[t] - act):
            correct_naive += 1
        ract = math.log(act / values[t])
        for m in MODELS:
            lst = errors.setdefault(m, [])
            lst.append((preds[m] - ract) ** 2)
            del lst[:-MAX_MODEL_ERRORS]

    print("=" * 68)
    print(f"BACKTEST h=1  (n={len(abs_err)} días hábiles, últimos {len(abs_err)})")
    print("=" * 68)
    print(f"  MAE autopilot : {mean(abs_err):.4f} Bs")
    print(f"  MAE naive     : {mean(naive_err):.4f} Bs")
    print(f"  MAPE          : {mean(pct_err):.4f} %")
    print(f"  Mejora vs naive: {100*(1-mean(abs_err)/mean(naive_err)):.1f}%")
    print(f"  Mejor que naive en {100*correct_naive/len(abs_err):.1f}% de los días")

    # --- h=5 y h=21 (phi calibrado en la primera mitad) ---
    print()
    print("BACKTEST h=5 / h=21 (modelo de produccion: blend + damping)")
    half = n // 2
    for h in (5, 21):
        phi = calibrate_phi(values[:half], h)
        ae, ae_naive = [], []
        for t in range(half, n - h):
            rh = ewma_last(r[max(0, t - 15):t], 3)
            med = horizon_median(values, t, h)
            pred = point_forecast(values[t], rh, h, phi, med)
            ae.append(abs(pred - values[t + h]))
            ae_naive.append(abs(values[t] - values[t + h]))
        print(
            f"  h={h:>2}: n={len(ae):<3} MAE={mean(ae):.4f} Bs  "
            f"(naive {mean(ae_naive):.4f})  phi={phi:.2f}"
        )
    return 0


def cmd_reset(args):
    if not args.yes:
        print("Esto borra state/. Volvé a correr con --yes.")
        return 1
    for f in (SERIES_CSV, STATE_JSON, FORECAST_JSON, METRICS_JSON, FLOG_CSV, EVENTS_LOG):
        try:
            os.remove(f)
        except OSError:
            pass
    print("Estado borrado. La próxima corrida re-siembra la historia.")
    return 0


def main():
    p = argparse.ArgumentParser(description="BCV Autopilot")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("run", help="loop autónomo")
    sub.add_parser("once", help="una pasada")
    sp = sub.add_parser("status", help="estado actual")
    sp.add_argument("--json", action="store_true")
    sub.add_parser("backtest", help="validación histórica")
    rp = sub.add_parser("reset", help="borrar estado")
    rp.add_argument("--yes", action="store_true")

    args = p.parse_args()
    cmd = args.cmd or "run"
    return {
        "run": cmd_run,
        "once": cmd_once,
        "status": cmd_status,
        "backtest": cmd_backtest,
        "reset": cmd_reset,
    }[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
