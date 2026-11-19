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


def convert_polygons(result, name, expected_breaks=None, direction="Unknown"):
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
        attributes["Direction"] = direction
        attributes["RepairedRings"] = repaired_count
        attributes["CollapsedParts"] = collapsed_count
        features.append({"type": "Feature", "properties": attributes, "geometry": mapping(geometry)})
    breaks = {float(f["properties"].get("ToBreak", -1)) for f in features}
    if not features:
        raise ValueError("No service-area polygons returned.")
    if expected_breaks is not None and any(
        not any(abs(value - actual) < 1e-6 for actual in breaks)
        for value in expected_breaks
    ):
        raise ValueError("Some requested time ranges were not returned. Inspect the raw response.")
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


DIRECTIONS = {"From site": "esriNATravelDirectionFromFacility",
              "To site": "esriNATravelDirectionToFacility"}


def parse_breaks(text):
    import math
    try:
        values = [float(part.strip()) for part in text.split(",")]
    except (ValueError, AttributeError):
        raise ValueError("Enter comma-separated minutes, for example: 45,60,90.") from None
    if not values or any(not math.isfinite(v) or not 0 < v < 300 for v in values):
        raise ValueError("Every drive time must be greater than 0 and less than 300 minutes.")
    return sorted(set(values))


def calculate_catchments(key, name, lon, lat, breaks, direction, mode, raw):
    """Retain each response immediately, including on a partial Both failure."""
    import json
    facilities = {"geometryType": "esriGeometryPoint", "spatialReference": {"wkid": 4326},
                  "features": [{"geometry": {"x": lon, "y": lat}, "attributes": {"Name": name}}]}
    features = []
    for label in (list(DIRECTIONS) if direction == "Both" else [direction]):
        result = call_api("solveServiceArea", key, facilities=json.dumps(facilities),
                          travelMode=json.dumps(mode), defaultBreaks=",".join(format(v, "g") for v in breaks),
                          travelDirection=DIRECTIONS[label], outputPolygons="esriNAOutputPolygonSimplified",
                          splitPolygonsAtBreaks="false", overlapPolygons="true",
                          returnFacilities="true", outSR="4326")
        raw["responses"].append({"direction": label, "response": result})
        features.extend(convert_polygons(result, name, breaks, label)["features"])
    return {"data": {"type": "FeatureCollection", "features": features},
            "name": name, "lon": lon, "lat": lat}


def restore_saved(raw):
    if raw.get("format") != "truck-catchments-v2":
        # A legacy raw response does not record travel direction reliably.
        return recover_response(raw)
    recovered = []
    for entry in raw.get("responses", []):
        item = recover_response(entry["response"])
        label = entry.get("direction", "Unknown")
        for feature in item["data"]["features"]:
            feature["properties"]["Direction"] = label
        recovered.append(item)
    if not recovered:
        raise ValueError("No saved responses found.")
    first = recovered[0]
    if any(abs(r["lon"]-first["lon"]) > 1e-7 or abs(r["lat"]-first["lat"]) > 1e-7 for r in recovered):
        raise ValueError("Saved responses must refer to the same site.")
    first["data"]["features"] = [f for r in recovered for f in r["data"]["features"]]
    return first


def web_app():
    import json
    import html
    import folium
    import streamlit as st
    import streamlit.components.v1 as components

    st.set_page_config(page_title="Truck Catchment Explorer", page_icon="🌍", layout="wide")
    st.title("Truck Catchment Explorer")
    st.caption("Esri truck routing • Custom drive times • From / To / Both • GeoJSON for QGIS")
    key = os.environ.get("ESRI_API_KEY", "")
    if not key:
        try:
            key = st.secrets.get("ESRI_API_KEY", "")
        except FileNotFoundError:
            key = ""
    with st.sidebar:
        st.header("Site and routing input")
        with st.form("calculate"):
            name = st.text_input("Site name", "WRRP Site", max_chars=100)
            lon = st.number_input("Longitude", min_value=-180.0, max_value=180.0,
                                  value=-97.13540, format="%.5f", step=0.00001)
            lat = st.number_input("Latitude", min_value=-90.0, max_value=90.0,
                                  value=49.69110, format="%.5f", step=0.00001)
            times = st.text_input("Drive times (minutes)", "45,60,90",
                                 help="Comma-separated values greater than 0 and less than 300.", max_chars=200)
            direction = st.selectbox("Travel direction", ["From site", "To site", "Both"])
            submitted = st.form_submit_button("Calculate catchments", type="primary")
        st.caption("From: leaving the site. To: arriving at the site. Both: two separate analyses.")
        st.caption("Each polygon covers the full area within its time limit (for example 0–45, 0–60, 0–90 minutes).")
        st.caption("Trucking Time • Default truck profile • No departure/arrival time or custom vehicle dimensions.")
        st.caption("Both makes two service-area requests and uses more Esri allowance. Downloads do not recalculate.")

    with st.expander("Restore saved Esri JSON — no API request"):
        uploaded = st.file_uploader("Upload saved Esri JSON", type=["json"])
        restore = st.button("Load saved result", disabled=uploaded is None)
    if restore and uploaded is not None:
        for field in ("result", "problem", "raw"):
            st.session_state.pop(field, None)
        try:
            raw = json.loads(uploaded.getvalue())
            if not isinstance(raw, dict):
                raise ValueError("Upload a complete response object.")
            st.session_state["raw"] = raw
            st.session_state["result"] = restore_saved(raw)
        except (ValueError, KeyError, TypeError) as exc:
            st.session_state["problem"] = "Cannot import this response: " + str(exc)
    if submitted:
        for field in ("result", "problem", "raw"):
            st.session_state.pop(field, None)
        try:
            breaks = parse_breaks(times)
            if not key:
                raise ValueError("Set ESRI_API_KEY in Streamlit Secrets or run python app.py locally.")
            with st.spinner("Calculating truck catchments..."):
                if "truck_mode" not in st.session_state:
                    modes = call_api("retrieveTravelModes", key).get("supportedTravelModes", [])
                    mode = next((m for m in modes if m.get("name") == "Trucking Time"), None)
                    if mode is None:
                        raise ValueError("Trucking Time is unavailable for this credential.")
                    st.session_state["truck_mode"] = mode
                raw = {"format": "truck-catchments-v2", "requested_breaks": breaks,
                       "requested_direction": direction, "polygon_type": "cumulative", "responses": []}
                st.session_state["raw"] = raw
                st.session_state["result"] = calculate_catchments(
                    key, name, lon, lat, breaks, direction, st.session_state["truck_mode"], raw)
        except (RuntimeError, ValueError) as exc:
            message = str(exc)
            st.session_state["problem"] = message.replace(key, "[redacted]") if key else message

    if "problem" in st.session_state:
        st.error(st.session_state["problem"])
        st.caption("If a request already completed, save its raw JSON below before retrying. A Both run may contain one completed direction.")
    if "raw" in st.session_state:
        raw = st.session_state["raw"]
        with st.expander("Esri service messages and raw response"):
            if raw.get("format") == "truck-catchments-v2":
                for entry in raw["responses"]:
                    st.write(entry["direction"], entry["response"].get("messages", []))
            else:
                st.write(raw.get("messages", []))
            st.download_button("Download raw Esri JSON", json.dumps(raw),
                               file_name="esri_response.json", mime="application/json")
    if "result" not in st.session_state:
        if "problem" not in st.session_state:
            st.info("Enter a site, choose drive times and direction, and click Calculate catchments.")
        return
    saved = st.session_state["result"]
    data = saved["data"]
    features = data["features"]
    breaks = sorted({float(f["properties"]["ToBreak"]) for f in features})
    labels = sorted({f["properties"].get("Direction", "Unknown") for f in features})
    repairs = sum(f["properties"].get("RepairedRings", 0) for f in features)
    if repairs:
        st.info("Repaired {} boundary ring(s) during conversion; no routing recalculation was needed.".format(repairs))
    if any(f["properties"].get("CollapsedParts", 0) for f in features):
        st.warning("Collapsed line/point parts were omitted from polygon output. Keep the raw JSON for review.")
    st.success("Catchments ready for {} ({:.5f}, {:.5f}).".format(saved["name"], saved["lon"], saved["lat"]))
    suffix = "_".join(format(v, "g") for v in breaks)
    direction_tag = "both" if len(labels) > 1 else labels[0].split()[0].lower()
    st.download_button("Download GeoJSON for QGIS", json.dumps(data),
                       file_name="truck_catchments_{}_{}.geojson".format(direction_tag, suffix),
                       mime="application/geo+json")
    if "Unknown" in labels:
        st.warning("This legacy response does not record direction. Its original ring/disk geometry is preserved.")
    palette = ["#dc2626", "#f59e0b", "#2563eb", "#16a34a", "#9333ea", "#0891b2"]
    colors = {v: palette[i % len(palette)] for i, v in enumerate(breaks)}
    st.caption("Time limits: " + " / ".join(format(v, "g") + " min" for v in breaks)
               + ". From: solid outlines. To: dashed outlines. Use map layers to compare directions and times.")
    m = folium.Map(location=[saved["lat"], saved["lon"]], zoom_start=7, tiles="OpenStreetMap")
    bounds = []
    for label in labels:
        for value in reversed(breaks):
            group = {"type": "FeatureCollection", "features": [f for f in features
                     if f["properties"].get("Direction", "Unknown") == label
                     and float(f["properties"]["ToBreak"]) == value]}
            if not group["features"]:
                continue
            def style(feature, color=colors[value], dashed=label == "To site"):
                return {"color": color, "weight": 2.5, "fillOpacity": 0.08,
                        "dashArray": "7 5" if dashed else None}
            layer = folium.GeoJson(group, name="{} — {} min".format(label, format(value, "g")),
                style_function=style, tooltip=folium.GeoJsonTooltip(
                    fields=["Direction", "FromBreak", "ToBreak"],
                    aliases=["Direction", "Lower bound (min)", "Upper bound (min)"]))
            layer.add_to(m)
            bounds.extend(layer.get_bounds())
    folium.CircleMarker([saved["lat"], saved["lon"]], radius=6, color="#ea580c", fill=True,
                        fill_opacity=1, tooltip=html.escape(saved["name"])).add_to(m)
    if bounds:
        m.fit_bounds(bounds)
    folium.LayerControl(collapsed=False).add_to(m)
    components.html(m.get_root().render(), height=620)
    st.caption("Routing: Esri. Basemap: © OpenStreetMap contributors. Network coverage varies by location.")


if __name__ == "__main__":
    # Streamlit executes scripts inside its runtime; plain python uses the local launcher.
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    if get_script_run_ctx(suppress_warning=True) is not None:
        web_app()
    else:
        launch()
