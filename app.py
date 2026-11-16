"""Local Esri truck catchment web app. Run with: python app.py"""
import os
import sys


def launch():
    from getpass import getpass
    from pathlib import Path
    import subprocess

    key = getpass("Paste Esri API key (hidden): ").strip()
    if not key:
        raise SystemExit("No API key entered.")
    env = os.environ.copy()
    env["ESRI_API_KEY"] = key
    env["WRRP_WEB_RUNNING"] = "1"
    print("Open http://localhost:8501 in your browser. Press Control+C to stop.")
    try:
        subprocess.run([
            sys.executable, "-m", "streamlit", "run", str(Path(__file__).resolve()),
            "--server.address", "127.0.0.1", "--server.port", "8501",
            "--browser.gatherUsageStats", "false",
        ], env=env, check=True)
    except KeyboardInterrupt:
        pass


BASE = "https://route-api.arcgis.com/arcgis/rest/services/World/ServiceAreas/NAServer/ServiceArea_World"


def call_api(operation, key, **params):
    import json
    import requests

    try:
        response = requests.post(BASE + "/" + operation,
                                 data=dict(params, f="json", token=key),
                                 timeout=(15, 180))
        response.raise_for_status()
        result = response.json()
    except requests.exceptions.SSLError:
        raise RuntimeError("HTTPS certificate verification failed. Repair Python certificates.") from None
    except requests.exceptions.Timeout:
        raise RuntimeError("The request timed out. Esri may still have processed it; do not repeatedly retry.") from None
    except requests.exceptions.HTTPError as exc:
        raise RuntimeError("Esri HTTP status: {}".format(exc.response.status_code)) from None
    except (requests.exceptions.RequestException, json.JSONDecodeError):
        raise RuntimeError("Connection failed or the server returned an unreadable response.") from None
    if "error" in result:
        error = result["error"]
        message = "Esri error {}: {}".format(error.get("code"), error.get("message"))
        message += " " + " ".join(map(str, error.get("details", [])))
        raise RuntimeError(message.replace(key, "[redacted]"))
    return result


def convert_polygons(result, name):
    """Repair self-touching rings and preserve even-odd polygon interiors."""
    import math
    from shapely import make_valid
    from shapely.geometry import Polygon, MultiPolygon, mapping
    from shapely.geometry.polygon import orient
    from shapely.ops import unary_union

    def polygon_parts(geometry):
        if geometry.geom_type == "Polygon":
            return [geometry] if not geometry.is_empty else []
        if hasattr(geometry, "geoms"):
            return [p for child in geometry.geoms for p in polygon_parts(child)]
        return []

    feature_set = result.get("saPolygons", {})
    sr = feature_set.get("spatialReference", {})
    if sr and sr.get("latestWkid", sr.get("wkid")) != 4326:
        raise ValueError("Only WGS 84 (EPSG:4326) responses are supported.")
    features = []
    for source in feature_set.get("features", []):
        rings = source.get("geometry", {}).get("rings", [])
        if not rings:
            raise ValueError("A returned feature has no polygon rings.")
        geometry = None
        repaired_count = 0
        collapsed_count = 0
        for ring in rings:
            if len(ring) < 4 or any(len(c) < 2 or not all(math.isfinite(v) for v in c[:2]) for c in ring):
                raise ValueError("A ring contains missing or non-finite coordinates.")
            polygon = Polygon([c[:2] for c in ring])
            if not polygon.is_valid:
                repaired_count += 1
                polygon = make_valid(polygon)
            parts = polygon_parts(polygon)
            if not parts:
                collapsed_count += 1
                continue
            if polygon.geom_type == "GeometryCollection":
                collapsed_count += 1
            polygon = unary_union(parts)
            # XOR retains holes and disconnected islands regardless of ring order.
            geometry = polygon if geometry is None else geometry.symmetric_difference(polygon)
        if geometry is None or geometry.is_empty:
            raise ValueError("No polygon area remains after conversion. Inspect the raw response.")
        if not geometry.is_valid:
            geometry = make_valid(geometry)
        parts = polygon_parts(geometry)
        geometry = unary_union(parts)
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("Polygon conversion failed. Keep the raw response for inspection.")
        if geometry.geom_type == "Polygon":
            geometry = orient(geometry, sign=1)
        elif geometry.geom_type == "MultiPolygon":
            geometry = MultiPolygon([orient(p, sign=1) for p in geometry.geoms])
        else:
            raise ValueError("Unexpected geometry type.")
        attributes = dict(source.get("attributes", {}))
        attributes["SiteName"] = name
        attributes["RepairedRings"] = repaired_count
        attributes["CollapsedParts"] = collapsed_count
        features.append({"type": "Feature", "properties": attributes, "geometry": mapping(geometry)})
    breaks = {float(f["properties"].get("ToBreak", -1)) for f in features}
    if not {90, 120}.issubset(breaks):
        raise ValueError("Both 90 and 120 minute ranges were not returned. Inspect the raw response.")
    return {"type": "FeatureCollection", "features": features}


def recover_response(result):
    """Load a previously downloaded response without any API request."""
    facilities = result.get("facilities", {}).get("features", [])
    if len(facilities) != 1:
        raise ValueError("Import requires a response containing exactly one facility.")
    facility = facilities[0]
    point = facility.get("geometry", {})
    lon, lat = float(point["x"]), float(point["y"])
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("The imported site coordinates must be WGS 84 longitude/latitude.")
    name = str(facility.get("attributes", {}).get("Name") or "Imported site")
    return {"data": convert_polygons(result, name), "name": name, "lon": lon, "lat": lat}


def web_app():
    import json
    import html
    import folium
    import streamlit as st
    import streamlit.components.v1 as components

    st.set_page_config(page_title="Truck Catchment Explorer", page_icon="🌍", layout="wide")
    st.title("Truck Catchment Explorer")
    st.caption("Esri Routing • 90 / 120 minutes • GeoJSON for QGIS")
    key = os.environ.get("ESRI_API_KEY", "")

    with st.sidebar:
        st.header("Site input")
        with st.form("calculate"):
            name = st.text_input("Site name", "WRRP Site", max_chars=100)
            lon = st.number_input("Longitude", min_value=-180.0, max_value=180.0,
                                  value=-97.13540, format="%.5f", step=0.00001)
            lat = st.number_input("Latitude", min_value=-90.0, max_value=90.0,
                                  value=49.69110, format="%.5f", step=0.00001)
            submitted = st.form_submit_button("Calculate catchments", type="primary")
        st.caption("Trucking Time • Travel TO the site • Default truck profile")
        st.caption("Output: 0–90 min and 90–120 min rings. Combine both for the full 0–120 min area.")
        st.caption("No departure/arrival time or custom vehicle dimensions are set. Network coverage varies by location.")
        st.caption("Calculations use your Esri account's service allowance or billing. Downloads do not repeat the calculation.")

    with st.expander("Restore saved Esri JSON — no API request", expanded=False):
        uploaded = st.file_uploader("Upload esri_response.json", type=["json"])
        restore = st.button("Load saved result", disabled=uploaded is None)
    if restore and uploaded is not None:
        st.session_state.pop("result", None)
        st.session_state.pop("problem", None)
        try:
            result = json.loads(uploaded.getvalue())
            if not isinstance(result, dict):
                raise ValueError("Upload a complete Esri response JSON object.")
            st.session_state["raw"] = result
            st.session_state["result"] = recover_response(result)
        except (ValueError, KeyError, TypeError) as exc:
            st.session_state["problem"] = "Cannot import this response: " + str(exc)

    if submitted:
        st.session_state.pop("result", None)
        st.session_state.pop("raw", None)
        st.session_state.pop("problem", None)
        with st.spinner("Requesting Esri truck catchments. Please wait..."):
            try:
                if not key:
                    raise ValueError("Restart with python app.py and enter an API key to calculate.")
                if "truck_mode" not in st.session_state:
                    modes = call_api("retrieveTravelModes", key).get("supportedTravelModes", [])
                    mode = next((m for m in modes if m.get("name") == "Trucking Time"), None)
                    if mode is None:
                        raise ValueError("Trucking Time is unavailable for this credential.")
                    st.session_state["truck_mode"] = mode
                facilities = {"geometryType": "esriGeometryPoint", "spatialReference": {"wkid": 4326},
                              "features": [{"geometry": {"x": lon, "y": lat}, "attributes": {"Name": name}}]}
                result = call_api("solveServiceArea", key,
                                  facilities=json.dumps(facilities),
                                  travelMode=json.dumps(st.session_state["truck_mode"]),
                                  defaultBreaks="90,120",
                                  travelDirection="esriNATravelDirectionToFacility",
                                  outputPolygons="esriNAOutputPolygonSimplified",
                                  splitPolygonsAtBreaks="true", overlapPolygons="true",
                                  returnFacilities="true", outSR="4326")
                st.session_state["raw"] = result
                data = convert_polygons(result, name)
                st.session_state["result"] = {"data": data, "name": name, "lon": lon, "lat": lat}
            except (RuntimeError, ValueError) as exc:
                st.session_state["problem"] = str(exc).replace(key, "[redacted]")

    if "problem" in st.session_state:
        st.error(st.session_state["problem"])
    if "raw" in st.session_state:
        with st.expander("Esri service messages and raw response"):
            st.write(st.session_state["raw"].get("messages", []))
            st.download_button("Download raw Esri JSON", json.dumps(st.session_state["raw"]),
                               file_name="esri_response.json", mime="application/json")
    if "result" not in st.session_state:
        if "problem" not in st.session_state:
            st.info("Calculate a site, or restore a saved Esri JSON above without a new API request.")
        return

    saved = st.session_state["result"]
    data = saved["data"]
    repairs = sum(f["properties"].get("RepairedRings", 0) for f in data["features"])
    collapsed = sum(f["properties"].get("CollapsedParts", 0) for f in data["features"])
    if repairs:
        st.info("Repaired {} self-intersecting or self-touching boundary ring(s). No routing recalculation was performed during conversion.".format(repairs))
    if collapsed:
        st.warning("{} ring(s) contained collapsed line/point parts omitted from polygon output. Keep the raw JSON for review.".format(collapsed))
    st.success("Catchments ready for {} ({:.5f}, {:.5f}).".format(saved["name"], saved["lon"], saved["lat"]))
    st.download_button("Download GeoJSON for QGIS", json.dumps(data),
                       file_name="truck_catchments_90_120.geojson", mime="application/geo+json")
    st.caption("Red: 0–90 minutes. Blue: 90–120 minutes. Orange marker: input site.")
    m = folium.Map(location=[saved["lat"], saved["lon"]], zoom_start=7, tiles="OpenStreetMap")
    layer = folium.GeoJson(data, name="Esri truck catchments",
                          style_function=lambda f: {"color": "#dc2626" if float(f["properties"]["ToBreak"]) == 90 else "#2563eb",
                                                    "weight": 2.5, "fillOpacity": 0.15},
                          tooltip=folium.GeoJsonTooltip(fields=["FromBreak", "ToBreak"],
                                                       aliases=["From (min)", "To (min)"]))
    layer.add_to(m)
    folium.CircleMarker([saved["lat"], saved["lon"]], radius=6, color="#ea580c", fill=True,
                        fill_opacity=1, tooltip=html.escape(saved["name"])).add_to(m)
    m.fit_bounds(layer.get_bounds())
    components.html(m.get_root().render(), height=620)
    st.caption("Routing: Esri. Basemap: © OpenStreetMap contributors. The map preview requires internet access.")


if __name__ == "__main__":
    if os.environ.get("WRRP_WEB_RUNNING") == "1":
        web_app()
    else:
        launch()
