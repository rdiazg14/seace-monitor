# Seguridad SEACE — inventario y rotación de secretos

Documento operativo. **Nunca pegues valores de secretos aquí ni en issues/chat.**
Si sospechás filtración: rotá primero, investigá después.

Estado de repos (sep 2026):

| Repo | GitHub | Notas |
|---|---|---|
| `seace-monitor` | público | pipeline + scripts |
| `seace-web` | público | SPA; anon key en bundle |
| `seace-ai-proxy` | **privado** | Worker chat/analizar/cotizar |
| `seace-pipeline-trigger` | **no existe remoto** | solo disco local + CF |

---

## 1. `seace-pipeline-trigger` sin git

**Hecho:** no hay `.git`, no hay `rdiazg14/seace-pipeline-trigger` en GitHub.
El Worker **sí** está en Cloudflare (`crons = ["0 14 * * *"]`) con secrets
`GITHUB_PAT` y `TRIGGER_TEST_TOKEN`. El código vive solo en la máquina de
Rolando: si se pierde el disco, no hay fuente para redeploy (salvo el bundle
ya desplegado en CF, que no es editable como repo).

**Conviene versionarlo** como repo **privado** (no suma minutos de Pages;
sí cuenta Actions si algún día tiene workflows — hoy no hace falta CI).

Pasos sugeridos para Rolando:

```bash
cd seace-pipeline-trigger
git init
git add .
# Confirmá que .gitignore cubre .dev.vars, .env, node_modules, .wrangler
git commit -m "Initial: Worker que dispara pipeline.yml"
gh repo create seace-pipeline-trigger --private --source=. --remote=origin --push
```

No hace falta tocar Cloudflare: el Worker ya desplegado sigue; los próximos
`wrangler deploy` salen del repo.

### PAT mínimo (fine-grained)

El Worker solo hace:

`POST /repos/rdiazg14/seace-monitor/actions/workflows/pipeline.yml/dispatches`

**Fine-grained PAT** (recomendado):

1. GitHub → Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate.
2. Resource owner: tu usuario.
3. Repository access: **Only select repositories** → `seace-monitor`.
4. Permissions → Repository:
   - **Actions: Read and write** (necesario para `workflow_dispatch`).
5. Expiration: 90 días (o lo que aceptes rotar).
6. Copiá el token **una sola vez** →
   `npx wrangler secret put GITHUB_PAT` (en el directorio del trigger).

**Classic PAT:** el endpoint exige scope `repo` (no hay scope classic solo
de Actions). Es más poder del necesario; evitarlo.

No generes el token desde un agente/chat: hacelo en el browser y pegalo
solo en `wrangler secret put`.

---

## 2. Inventario de secretos

Última actualización de metadatos: consulta `gh secret list` /
`wrangler secret list` (sep 2026). Las fechas “última vez” son las de
actualización en el almacén, no auditoría de uso.

| nombre | tipo | donde | consumidor | multi-lugar | rotable sin downtime? | última vez conocida |
|---|---|---|---|---|---|---|
| `GEMINI_API_KEY` | API Google AI | GH `seace-monitor` · CF `seace-ai-proxy` · `.env` local · `.dev.vars` proxy | pipeline OCR/embeddings · Worker chat/analizar/cotizar · scripts locales | **sí (3+)** | sí, con overlap breve | GH 2026-08-17 |
| `SUPABASE_SERVICE_KEY` | JWT `service_role` | GH `seace-monitor` · CF `seace-ai-proxy` · `.env` local | pipeline escritura BD · Worker (analisis/cotizar log/admin perfiles) · scripts | **sí (3)** | **no limpio** (ver §3) | GH 2026-08-15 |
| `SUPABASE_ANON_KEY` | JWT `anon` (público) | CF `seace-ai-proxy` · `.env` / `.dev.vars` · **hardcode** `seace-web/src/lib/supabase.ts` | Worker lecturas PostgREST · SPA · Auth API | sí (público) | n/a — pública por diseño | bundle web |
| `SUPABASE_URL` | URL proyecto | GH · CF `[vars]` · `.env` · hardcode web | todos | sí (no secreto fuerte) | sí | GH 2026-08-15 |
| `DATABASE_URL` | Postgres DSN | `.env` local · **referenciado** en `pipeline.yml` como `secrets.DATABASE_URL` | `run_sql.py`, backfills, scripts DDL | local (+ GH si existe) | sí | local; **no aparece en `gh secret list`** — verificar en UI / crear |
| `FUNNEL_TOKEN` | token opaco | GH · CF proxy · `.env` · `.dev.vars` | `GET /funnel-pendientes` · `reconciliar_funnel.py` · cron | **sí (3)** | sí (overlap) | GH 2026-08-21 |
| `ANALIZAR_SERVICE_TOKEN` | token opaco | GH · CF proxy · `.env` | `X-Service-Token` en `/analizar` (pipeline backfill) | **sí (3)** | sí (overlap) | GH 2026-09-06 |
| `GITHUB_PAT` | PAT GitHub | CF `seace-pipeline-trigger` only | cron/dispatch `pipeline.yml` | no | sí | CF secret |
| `TRIGGER_TEST_TOKEN` | token opaco | CF trigger only | `POST /` de prueba del trigger | no | sí | CF secret |
| `GITHUB_TOKEN` | token Actions | inyectado por GH Actions | `alerta_g3.py` / `gh` en workflows | efímero | n/a | por run |

**Brecha operativa:** `pipeline.yml` y `clasificacion_semanal.yml`
referencian `secrets.DATABASE_URL`, pero `gh secret list` en
`seace-monitor` **no lista** `DATABASE_URL`. DDL/`run_sql.py` en Actions
siguen necesitando el secret. Los writers de capa 3
(`clasificacion_capa.escribir_keyword` / `escribir_gemini`) **caen a
supabase-py** con `SUPABASE_SERVICE_KEY` si el DSN falta; el trigger de
eco sigue disparándose por PostgREST.

---

## 3. Procedimiento de rotación (sin romper prod)

Regla general: **almacén nuevo primero, consumidores después, revocar
viejo al final**. Los que están en varios lados se rotan **en el mismo
mantenimiento**.

### 3.1 `ANALIZAR_SERVICE_TOKEN` / `FUNNEL_TOKEN` (fáciles)

1. Generá un valor nuevo (password manager / `openssl rand -hex 32`).
2. `npx wrangler secret put …` en `seace-ai-proxy`.
3. GitHub → `seace-monitor` → Actions secrets → Update.
4. Actualizá `.env` local.
5. Smoke:
   - service: `POST /analizar` con `X-Service-Token` → 200 (no 401).
   - funnel: `GET /funnel-pendientes` con Bearer → 200.
6. Listo. Downtime ≈ 0 si el put de CF y GH son seguidos.

### 3.2 `GEMINI_API_KEY`

1. Google AI Studio / Cloud → crear API key nueva (no borres la vieja aún).
2. CF `seace-ai-proxy`: `wrangler secret put GEMINI_API_KEY`.
3. GH Actions secret + `.env` / `.dev.vars`.
4. Smoke: chat logueado + un `/analizar` MISS (o script local corto).
5. Revocá la key vieja en Google.
6. Si filtró: revocá **ya** la vieja; aceptá minutos de 503 hasta poner la nueva.

### 3.3 `SUPABASE_SERVICE_KEY` (la peor)

Bypass total de RLS. Si filtró, tratá como incidente.

**Rotación planificada (Supabase Dashboard → Project Settings → API):**

1. Anotá la hora. Avisá: pipeline y admin pueden fallar ~minutos.
2. En Supabase, **roll / regenerate** `service_role` (el UI genera un JWT nuevo;
   el `anon` puede quedarse).
3. **Inmediatamente** actualizá en este orden:
   1. CF `SUPABASE_SERVICE_KEY` (`wrangler secret put`)
   2. GH Actions `SUPABASE_SERVICE_KEY`
   3. `.env` local
4. Smoke: `POST /analizar` (persistencia BD), `GET /admin/stats` con JWT admin,
   un run corto de ingesta o `run_sql` no-op.
5. Confirmá que el JWT viejo ya no autentica (PostgREST 401).

**Si filtró (orden de emergencia):**

1. Regenerá `service_role` en Supabase **ya** (invalida la filtrada).
2. Actualizá CF + GH + `.env` en los siguientes 5 minutos.
3. Revisá logs de Supabase / CF por escrituras raras desde la filtración.
4. No hace falta rotar `anon` solo por esto (salvo que también se filtrara
   algo más).

### 3.4 `DATABASE_URL`

1. Supabase → Database → reset password del rol (o connection string nueva).
2. Actualizá `.env` y el secret GH `DATABASE_URL` (crealo si falta).
3. Smoke: `uv run python scripts/run_sql.py docs/capas_fase2_poblar.sql`
   (debe ser no-op).
4. El pipeline que use `DATABASE_URL` debe pasar el siguiente schedule.

### 3.5 `GITHUB_PAT` (pipeline-trigger)

1. Creá fine-grained nuevo (§1).
2. `cd seace-pipeline-trigger && npx wrangler secret put GITHUB_PAT`
3. Probá: `POST /` del trigger con `TRIGGER_TEST_TOKEN` → `github_status` 204/200.
4. Revocá el PAT viejo en GitHub.
5. El cron 14:00 UTC del día siguiente confirma.

### 3.6 `TRIGGER_TEST_TOKEN`

1. Generá valor nuevo → `wrangler secret put TRIGGER_TEST_TOKEN`.
2. Actualizá tu nota local / password manager.
3. Curl de prueba del README del trigger.

### 3.7 `SUPABASE_ANON_KEY` — por qué no se “rota por exposición”

La anon key **viaja en el bundle del frontend a propósito**. Quien la tenga
solo obtiene lo que RLS + grants permiten. Rotarla implica:

1. Nuevo JWT anon en Supabase.
2. Actualizar `seace-web/src/lib/supabase.ts` + redeploy Pages.
3. `wrangler secret put SUPABASE_ANON_KEY` en el proxy.
4. `.env` / docs placeholders.

Solo rota anon si sospechás compromiso del **proyecto** entero o regeneraste
todas las keys del dashboard. **No** la trates como secreto de servidor.

---

## 4. Checklist post-filtración (resumen)

| filtró | acción inmediata |
|---|---|
| `SUPABASE_SERVICE_KEY` | Regenerar service_role → CF + GH + `.env` → auditar escrituras |
| `GEMINI_API_KEY` | Revocar en Google → put nueva en CF + GH + `.env` |
| `DATABASE_URL` | Reset password Postgres → `.env` + GH |
| `FUNNEL_TOKEN` / `ANALIZAR_SERVICE_TOKEN` | Put nuevos en CF + GH + `.env` |
| `GITHUB_PAT` | Revocar PAT → put nuevo en CF trigger |
| `SUPABASE_ANON_KEY` | Solo si rotás el proyecto; priorizá revisar **RLS** |

---

## 5. Controles ya aplicados (contexto)

- RLS en `migraciones_datos` y snapshots (`docs/sec_rls_faltante.sql`).
- Worker: chat / analizar / cotizar exigen JWT de sesión (o `X-Service-Token`).
- Secret scanning + push protection: activar en repos **públicos**
  (`seace-monitor`, `seace-web`) → Settings → Code security.

---

## 6. Placeholder en documentación

En ejemplos de Markdown usá `<ANON_KEY>`, nunca un JWT real. La key del
bundle web es pública; pegarla en docs solo confunde y ensucia el historial.
