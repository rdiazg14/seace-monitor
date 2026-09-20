"""Paquete compartido del pipeline SEACE Monitor.

Centraliza lo que antes estaba duplicado script por script: carga de `.env`,
cliente Supabase, acceso a Postgres (psycopg), cliente Gemini y clasificación.
Los entrypoints de la raíz importan de acá; la lógica de negocio no cambia.
"""
