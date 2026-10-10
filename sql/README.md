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

La cadena de instalacion/restauracion y migraciones forward-only se mantiene en el repositorio privado del esquema; aplicar cada cambio por canal trazable con prerequisitos, respaldo y verificacion. No ejecutar SQL historico por estar en esta carpeta. El orden de la
serie histórica está documentado en el workspace privado; no ejecutar los
archivos en orden alfabético.
