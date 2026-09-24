# Herramientas de diagnóstico y deuda

Esta carpeta reúne evaluaciones, diagnósticos, backfills y migraciones manuales que no forman parte del pipeline ordinario. Algunos scripts leen producción; otros escriben datos, reemplazan chunks o consumen APIs.

Antes de ejecutar un archivo:

1. Buscar consumidores en código y workflows.
2. Leer argumentos, variables y operaciones de persistencia.
3. Identificar si es diagnóstico, evaluación, backfill o migración.
4. Preparar respaldo, límite y recuperación cuando escriba.
5. Registrar la evidencia en la tarea correspondiente.

No ejecutar scripts por su nombre ni importarlos como smoke test. La revisión para archivar herramientas de una sola ejecución es REF-004. El estado vive en `../../docs/BACKLOG.md`.
