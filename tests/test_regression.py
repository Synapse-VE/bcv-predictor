"""Tests de regresión del BCV Autopilot.

Convención del proyecto: todo bug de producción se corrige con un test que falle
antes del fix y quede en la suite.

Caso real (25/10/2026):
  - Última publicada: FV 27/10/2026 (martes), 887,302.
  - Vigente el domingo 25/10: FV 23/10/2026 (viernes), 886,2271.
  - El lunes 26/10 es feriado: no hay tasa ni publicación.
  - El próximo pronóstico es para el 28/10, no para "mañana".
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autopilot as ap  # noqa: E402


def fila(d, usd):
    return {"date": d, "usd": usd, "bid": usd * 0.9975, "eur": None, "cny": None,
            "try": None, "rub": None, "src": "test"}


class TestTasaVigenteYProxima(unittest.TestCase):
    def setUp(self):
        self.hoy = date(2026, 10, 25)  # domingo
        self.feriados = {date(2026, 10, 26)}  # lunes feriado
        self.rows = [
            fila(date(2026, 10, 22), 884.7496),
            fila(date(2026, 10, 23), 886.2271),
            fila(date(2026, 10, 27), 887.3020),  # ya publicada, rige el martes
        ]

    def test_vigente_es_la_ultima_fv_no_futura(self):
        vigente, proxima = ap.tasa_vigente_y_proxima(self.rows, self.hoy, self.feriados)
        self.assertIsNotNone(vigente)
        self.assertEqual(vigente["date"], date(2026, 10, 23))
        self.assertAlmostEqual(vigente["usd"], 886.2271)
        self.assertIsNotNone(proxima)
        self.assertEqual(proxima["date"], date(2026, 10, 27))
        self.assertAlmostEqual(proxima["usd"], 887.3020)

    def test_sin_tasa_futura_la_proxima_es_none(self):
        vigente, proxima = ap.tasa_vigente_y_proxima(self.rows[:2], self.hoy, self.feriados)
        self.assertEqual(vigente["date"], date(2026, 10, 23))
        self.assertIsNone(proxima)

    def test_payload_expone_vigente_proxima_y_fecha_del_pronostico(self):
        state = ap.new_state()
        payload = ap.build_forecast_payload(state, self.rows, self.feriados, today=self.hoy)

        self.assertEqual(payload["today"], "2026-10-25")
        self.assertEqual(payload["vigente"]["date"], "2026-10-23")
        self.assertAlmostEqual(payload["vigente"]["usd"], 886.2271)
        self.assertEqual(payload["proxima"]["date"], "2026-10-27")
        self.assertAlmostEqual(payload["proxima"]["usd"], 887.3020)
        # El próximo pronóstico cae el 28/10 (el 26 es feriado y el 27 es la próxima).
        self.assertEqual(payload["official"]["1"]["date"], "2026-10-28")

    def test_add_bd_salta_el_feriado(self):
        self.assertEqual(ap.add_bd(date(2026, 10, 23), 1, self.feriados), date(2026, 10, 27))
        self.assertEqual(ap.add_bd(date(2026, 10, 27), 1, self.feriados), date(2026, 10, 28))

    def test_cambio_porcentual_separa_vigente_de_proxima(self):
        state = ap.new_state()
        payload = ap.build_forecast_payload(state, self.rows, self.feriados, today=self.hoy)
        # Vigente 23/10 vs anterior 22/10
        self.assertAlmostEqual(payload["vigente"]["change_pct"], (886.2271 / 884.7496 - 1) * 100, places=6)
        # Próxima 27/10 vs vigente 23/10
        self.assertAlmostEqual(payload["proxima"]["change_pct"], (887.3020 / 886.2271 - 1) * 100, places=6)


if __name__ == "__main__":
    unittest.main()
