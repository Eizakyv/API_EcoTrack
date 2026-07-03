# API EcoTrack

API para verificar la ubicación de usuarios en senderos y líneas de alta tensión.

## Variables de entorno requeridas

- `DB_HOST`: Host de PostgreSQL
- `DB_PORT`: Puerto de PostgreSQL (5432 por defecto)
- `DB_NAME`: Nombre de la base de datos
- `DB_USER`: Usuario de PostgreSQL
- `DB_PASSWORD`: Contraseña de PostgreSQL

## Endpoint

### POST /check

**Body (JSON):**
```json
{
  "latitude": 8.987911,
  "longitude": -79.546003
}