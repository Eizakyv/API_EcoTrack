from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
from waitress import serve
import os
import json
from shapely.geometry import Point, shape
from shapely.ops import transform, unary_union
import pyproj

app = Flask(__name__)

# ============================================================
# DATABASE CONFIGURATION
# ============================================================
DATABASE_URL = os.environ['DATABASE_URL']

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# Thresholds in meters
TRAIL_THRESHOLD_METERS = 15.0
POWER_LINE_THRESHOLD_METERS = 30.0
VALID_TRAIL_TYPES = ('Sendero Actual', 'Sendero', 'Carretera')

# ============================================================
# CARGA DE CAPAS GEOJSON (para parque y zona investigación)
# ============================================================
wgs84 = pyproj.CRS('EPSG:4326')
utm17n = pyproj.CRS('EPSG:32617')
project_to_meters = pyproj.Transformer.from_crs(wgs84, utm17n, always_xy=True).transform

park_geom = None          # polígono del parque (UTM)
research_geom = None      # polígono de zona de investigación (UTM)
research_wkt = None       # WKT de la zona de investigación (para SQL)

def load_geojson_polygon(file_path):
    """Carga un GeoJSON de polígono y lo proyecta a UTM."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        features = data.get('features', [])
        if not features:
            print(f"⚠️ No se encontraron features en {file_path}")
            return None
        geom = shape(features[0]['geometry'])
        if geom.geom_type not in ['Polygon', 'MultiPolygon']:
            print(f"⚠️ {file_path} no es un polígono, es {geom.geom_type}")
            return None
        # Transformar a UTM
        geom_utm = transform(project_to_meters, geom)
        return geom_utm
    except Exception as e:
        print(f"❌ Error al cargar {file_path}: {e}")
        return None

def get_research_zone_wkt():
    """Devuelve la zona de investigación en WKT (SRID 4326) para usarla en SQL."""
    try:
        with open("parcela_1ha_pnm.geojson", 'r', encoding='utf-8') as f:
            data = json.load(f)
        features = data.get('features', [])
        if not features:
            return None
        geom = shape(features[0]['geometry'])
        # Convertir a WKT en 4326
        from shapely.geometry import mapping
        from shapely.wkt import dumps
        return dumps(geom)
    except Exception as e:
        print(f"❌ Error al obtener WKT de zona investigación: {e}")
        return None

# Cargar capas al iniciar
try:
    park_geom = load_geojson_polygon("limites_pnm.geojson")
    research_geom = load_geojson_polygon("parcela_1ha_pnm.geojson")
    research_wkt = get_research_zone_wkt()
    print("✅ Capas de límites y zona de investigación cargadas.")
except Exception as e:
    print(f"❌ Error al cargar capas: {e}")

# ============================================================
# ENDPOINT /check
# ============================================================
@app.route('/check', methods=['POST'])
def check_location():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No se recibió JSON"}), 400

        lat = float(data.get('latitude'))
        lon = float(data.get('longitude'))
        point_wkt = f"POINT({lon} {lat})"

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # ----------------------------------------------------
        # 1. SENDERO MÁS CERCANO (fuera de zona de investigación)
        # ----------------------------------------------------
        # Si tenemos la zona de investigación, excluir senderos que estén dentro de ella
        if research_wkt is not None:
            cur.execute("""
                SELECT 
                    nombre,
                    ST_DistanceSpheroid(
                        ST_SetSRID(ST_GeomFromText(%s, 4326), 4326),
                        geom,
                        'SPHEROID["WGS 84",6378137,298.257223563]'
                    ) AS distancia
                FROM senderos
                WHERE tipo IN %s
                  AND NOT ST_Intersects(geom, ST_SetSRID(ST_GeomFromText(%s, 4326), 4326))
                ORDER BY distancia
                LIMIT 1
            """, (point_wkt, VALID_TRAIL_TYPES, research_wkt))
        else:
            # Si no hay zona de investigación, buscar el más cercano sin restricción
            cur.execute("""
                SELECT 
                    nombre,
                    ST_DistanceSpheroid(
                        ST_SetSRID(ST_GeomFromText(%s, 4326), 4326),
                        geom,
                        'SPHEROID["WGS 84",6378137,298.257223563]'
                    ) AS distancia
                FROM senderos
                WHERE tipo IN %s
                ORDER BY distancia
                LIMIT 1
            """, (point_wkt, VALID_TRAIL_TYPES))

        trail = cur.fetchone()

        # ----------------------------------------------------
        # 2. LÍNEA DE ALTA TENSIÓN MÁS CERCANA
        # ----------------------------------------------------
        cur.execute("""
            SELECT 
                ST_DistanceSpheroid(
                    ST_SetSRID(ST_GeomFromText(%s, 4326), 4326),
                    geom,
                    'SPHEROID["WGS 84",6378137,298.257223563]'
                ) AS distancia
            FROM lineas_tension
            ORDER BY distancia
            LIMIT 1
        """, (point_wkt,))
        power_line = cur.fetchone()

        cur.close()
        conn.close()

        # ====================================================
        # DISTANCIAS Y CLASIFICACIÓN
        # ====================================================
        trail_distance = trail['distancia'] if trail else None
        power_line_distance = power_line['distancia'] if power_line else None

        # --- Calcular distancia al parque y zona de investigación (con Shapely) ---
        user_point_wgs = Point(lon, lat)
        user_point_utm = transform(project_to_meters, user_point_wgs)

        park_distance = None
        is_inside_park = False
        if park_geom is not None:
            park_distance = user_point_utm.distance(park_geom)
            is_inside_park = user_point_utm.within(park_geom)
            if is_inside_park:
                park_distance = 0.0

        research_distance = None
        is_inside_research = False
        if research_geom is not None:
            research_distance = user_point_utm.distance(research_geom)
            is_inside_research = user_point_utm.within(research_geom)
            if is_inside_research:
                research_distance = 0.0

        # --- Clasificación (prioridad: peligro > advertencia > seguro) ---
        status = "seguro"
        message = "Se encuentra dentro del sendero."

        if power_line_distance is not None and power_line_distance < POWER_LINE_THRESHOLD_METERS:
            status = "peligro"
            message = "Cerca de línea de alta tensión."
        else:
            # Fuera del parque
            if not is_inside_park and park_distance is not None:
                status = "advertencia"
                message = f"Fuera del parque (distancia: {park_distance:.2f} m)"
            # Dentro de zona de investigación
            elif is_inside_research and research_distance is not None:
                status = "advertencia"
                message = f"Dentro de zona de investigación"
            # Fuera del sendero
            elif trail_distance is not None and trail_distance > TRAIL_THRESHOLD_METERS:
                status = "advertencia"
                message = f"Fuera del sendero (distancia: {trail_distance:.2f} m)"

        # ====================================================
        # RESPUESTA JSON
        # ====================================================
        response = {
            "status": status,
            "message": message,
            "location": {
                "latitude": lat,
                "longitude": lon
            },
            "trail": {
                "name": trail['nombre'] if trail else None,
                "distance_meters": round(trail_distance, 2) if trail_distance is not None else None
            },
            "powerLine": {
                "distance_meters": round(power_line_distance, 2) if power_line_distance is not None else None
            },
            "park": {
                "distance_meters": round(park_distance, 2) if park_distance is not None else None,
                "inside": is_inside_park
            },
            "researchZone": {
                "distance_meters": round(research_distance, 2) if research_distance is not None else None,
                "inside": is_inside_research
            }
        }

        print(f"\n📍 Location: {lat}, {lon} | Status: {status} | Msg: {message} | TrailDist: {trail_distance if trail_distance else 'N/A'}")

        return jsonify(response), 200

    except Exception as e:
        print(f"❌ Server error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Server started on http://0.0.0.0:{port}")
    serve(app, host='0.0.0.0', port=port)
