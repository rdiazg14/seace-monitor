"""Bootstrap para scripts de diagnóstico/eval archivados en deuda/.

Estos scripts vivían en la raíz del repo y se ejecutaban con `python <script>.py`
desde ahí, por lo que resolvían `seace_monitor.*` y a sus hermanos vía el cwd en
sys.path. Al archivarlos en deuda/, este módulo restaura ese comportamiento:

  - inserta la raíz del repo (para `seace_monitor.*` y módulos productivos),
  - inserta esta carpeta (para los imports entre hermanos, p. ej. eval_retrieval),
  - carga el .env de la raíz (que es donde vivía antes).

Cada script archivado lo importa como PRIMERA instrucción tras `from __future__`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DEUDA = Path(__file__).resolve().parent
_RAIZ = _DEUDA.parent

for _p in (_DEUDA, _RAIZ):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Cargar .env de la raíz (setdefault: no pisa variables ya definidas).
_env = _RAIZ / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip())
