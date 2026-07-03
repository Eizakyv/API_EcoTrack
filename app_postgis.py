from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
from waitress import serve
import os
from shapely.geometry import Point, shape
from shapely.ops import transform
from shapely.wkt import loads
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
# CARGA DE CAPAS DESDE POSTGIS (al iniciar)
# ============================================================
wgs84 = pyproj.CRS('EPSG:4326')
utm17n = pyproj.CRS('EPSG:32617')
project_to_meters = pyproj.Transformer.from_crs(wgs84, utm17n, always_xy=True).transform

park_geom = None          # polígono del parque (UTM)
research_geom = None      # polígono de zona de investigación (UTM)
plume_geom = None         # polígono de pluma grúa (UTM)

def load_geometry_from_db(table_name, geom_column='geom'):
    """
    Conecta a la base de datos, obtiene la primera geometría de la tabla
    (asumiendo que hay un solo registro o que el primero es el principal)
    y la convierte a objeto Shapely en UTM.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Obtener la geometría como WKT en SRID 4326
        cur.execute(f"SELECT ST_AsText({geom_column}) FROM {table_name} LIMIT 1;")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            wkt = row[0]
            geom_wgs = loads(wkt)
            if geom_wgs.is_empty:
                print(f"⚠️ Geometría vacía en {table_name}")
                return None
            # Proyectar a UTM (metros)
            geom_utm = transform(project_to_meters, geom_wgs)
            return geom_utm
        else:
            print(f"⚠️ No se encontró geometría en {table_name}")
            return None
    except Exception as e:
        print(f"❌ Error al cargar {table_name}: {e}")
        return None

# Cargar capas al iniciar
try:
    park_geom = load_geometry_from_db('limites_pnm')
    research_geom = load_geometry_from_db('parcela_1ha_pnm')
    plume_geom = load_geometry_from_db('pluma_grua_pnm')
    print("✅ Capas de límites, parcela de investigación y pluma cargadas desde la base de datos.")
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
        # 1. SENDERO MÁS CERCANO (excluyendo los que están dentro de la parcela investigación)
        # ----------------------------------------------------
        # Obtener la parcela de investigación en WKT (si existe) para la consulta SQL
        research_wkt = None
        if research_geom is not None:
            # Convertir la geometría UTM de vuelta a WGS84 para usar en SQL
            # Para simplificar, podemos usar la tabla directamente con ST_Intersects
            # Pero como ya tenemos la geometría en Shapely, podemos obtener su WKT en 4326
            # Hago una consulta separada para obtener el WKT desde la tabla
            try:
                conn2 = get_db_connection()
                cur2 = conn2.cursor()
                cur2.execute("SELECT ST_AsText(geom) FROM parcela_1ha_pnm LIMIT 1;")
                row = cur2.fetchone()
                cur2.close()
                conn2.close()
                if row and row[0]:
                    research_wkt = row[0]
            except Exception as e:
                print(f"⚠️ No se pudo obtener WKT de parcela: {e}")

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

        # --- Calcular distancias con Shapely ---
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

        plume_distance = None
        is_inside_plume = False
        if plume_geom is not None:
            plume_distance = user_point_utm.distance(plume_geom)
            is_inside_plume = user_point_utm.within(plume_geom)
            if is_inside_plume:
                plume_distance = 0.0

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
            # Dentro de pluma grúa (también advertencia, pero con mensaje específico)
            elif is_inside_plume and plume_distance is not None:
                status = "advertencia"
                message = f"Dentro de pluma grúa"
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
            },
            "plume": {
                "distance_meters": round(plume_distance, 2) if plume_distance is not None else None,
                "inside": is_inside_plume
            }
        }

        print(f"\n📍 Location: {lat}, {lon} | Status: {status} | Msg: {message}")

        return jsonify(response), 200

    except Exception as e:
        print(f"❌ Server error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Server started on http://0.0.0.0:{port}")
    serve(app, host='0.0.0.0', port=port)
