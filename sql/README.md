# sql/

Esquema y migraciones SQL de Supabase/PostgreSQL, clasificados por responsabilidad.

| Carpeta | Contenido |
|---|---|
| `esquema/` | Definición estructural base: tablas, vistas, políticas RLS |
| `funciones/` | Funciones RPC y vistas consultables (`create or replace`) |
| `migraciones/` | Cambios incrementales aplicados una sola vez (alter/drop/columnas) |
| `operacion/` | Runbooks de datos ejecutados manualmente (backfills) |

Las migraciones históricas de la serie `b*`/`c*`/`capas_fase*` y las
verificaciones viven en `docs/sql/` (ignorado por Git, privado del workspace).

Los SQL se aplican manualmente en el SQL Editor de Supabase. El orden de la
serie histórica está documentado en el workspace privado; no ejecutar los
archivos en orden alfabético.
