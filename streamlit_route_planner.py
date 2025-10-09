import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
st.set_page_config(page_title="Rain Gauge Monitor", layout="wide")

import pandas as pd
import json
import os
from geopy.distance import geodesic
import networkx as nx
import urllib.parse
import itertools
import math
import time
from typing import List, Dict, Tuple, Optional

@st.cache_data(ttl=300)
def load_sheet_days() -> pd.DataFrame:
    sa = st.secrets["google_service_account"]
    creds = Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sa["SHEET_ID"])
    ws = sh.worksheet(st.secrets["google_service_account"]["SHEET_TAB"])
    values = ws.get(st.secrets["google_service_account"]["SHEET_RANGE"])  # B..C

    rows = []
    for r in values:
        if len(r) < 2:
            continue
        station_id = (r[0] or "").strip()       # คอลัมน์ B
        days_raw = (r[1] or "").strip()         # คอลัมน์ C
        if not station_id:
            continue

        # แยกเลขออกจาก string เช่น "❌ 62 วัน" → 62
        import re
        match = re.search(r"(\d+)", days_raw)
        days_val = int(match.group(1)) if match else None

        rows.append({"station_id": station_id, "days_not_maintained": days_val})

    return pd.DataFrame(rows)
    
# ✅ ข้อมูลตำแหน่งฐาน
BASE_LOCATION = {
    'station_id': 'BASE01',
    'name_th': 'ศูนย์ปฏิบัติการ (ฐานหลัก)',
    'lat': 8.848510596250504,
    'lon': 98.80937422965278,
    'url': 'https://maps.app.goo.gl/6kMrzVxrXAnyNbiMA'
}

 

# ✅ Utility Functions (Define first)
def safe_float_conversion(value, default=0.0):
    """แปลงค่าเป็น float อย่างปลอดภัย"""
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (ValueError, TypeError):
        return default

def safe_get_station_name(df: pd.DataFrame, station_id: str, default: str = 'ไม่ทราบชื่อ') -> str:
    """ดึงชื่อสถานีอย่างปลอดภัย"""
    try:
        station_info = df[df['station_id'] == station_id]
        if not station_info.empty and 'name_th' in station_info.columns:
            name = station_info.iloc[0]['name_th']
            return str(name) if pd.notna(name) else default
        return default
    except Exception:
        return default

def cleanup_selected_stations():
    """ทำความสะอาด selected_stations"""
    try:
        if not st.session_state.get('include_base_location', False):
            # ลบ BASE_LOCATION ออกจาก selected_stations ถ้า checkbox ถูก uncheck
            if BASE_LOCATION['station_id'] in st.session_state.get('selected_stations', []):
                st.session_state.selected_stations.remove(BASE_LOCATION['station_id'])
        
        # ลบ duplicates และ None values
        if 'selected_stations' in st.session_state:
            stations = st.session_state.selected_stations
            st.session_state.selected_stations = list(set(filter(None, stations)))
    except Exception as e:
        st.error(f"ข้อผิดพลาดในการทำความสะอาดข้อมูล: {str(e)}")

def smart_rerun():
    """Rerun with intelligent delay to prevent race conditions"""
    try:
        if 'last_rerun_time' not in st.session_state:
            st.session_state.last_rerun_time = 0
        
        current_time = time.time()
        time_since_last = current_time - st.session_state.last_rerun_time
        
        if time_since_last < 0.5:  # ป้องกัน rapid rerun
            time.sleep(0.5 - time_since_last)
        
        st.session_state.last_rerun_time = time.time()
        st.rerun()
    except Exception as e:
        st.error(f"ข้อผิดพลาดในการรีเฟรช: {str(e)}")

def find_nearest_station_optimized(clicked_lat: float, clicked_lng: float, df: pd.DataFrame, 
                                 include_base: bool = False, max_distance_m: int = 500) -> Optional[str]:
    """หาสถานีที่ใกล้ที่สุดอย่างมีประสิทธิภาพ"""
    try:
        min_distance = float('inf')
        closest_station = None
        
        # ตรวจสอบสถานีปกติ
        for _, row in df.iterrows():
            lat_val = safe_float_conversion(row.get('lat'))
            lon_val = safe_float_conversion(row.get('lon'))
            
            if lat_val == 0.0 and lon_val == 0.0:
                continue
                
            try:
                distance = geodesic((clicked_lat, clicked_lng), (lat_val, lon_val)).meters
                if distance < min_distance and distance < max_distance_m:
                    min_distance = distance
                    closest_station = str(row['station_id'])
            except Exception:
                continue
        
        # ตรวจสอบฐานหลัก (ถ้าเลือก)
        if include_base:
            try:
                base_distance = geodesic(
                    (clicked_lat, clicked_lng), 
                    (BASE_LOCATION['lat'], BASE_LOCATION['lon'])
                ).meters
                if base_distance < min_distance and base_distance < max_distance_m:
                    closest_station = BASE_LOCATION['station_id']
            except Exception:
                pass
        
        return closest_station
    except Exception as e:
        st.error(f"ข้อผิดพลาดในการค้นหาสถานี: {str(e)}")
        return None

# ✅ Create Route Map Function (Define before main)
def create_route_map(route_info: List[Dict], path_coords: List[List[float]], total_distance: float):
    """สร้างแผนที่แสดงเส้นทาง"""
    try:
        import folium
    except ImportError:
        st.error("❌ ไม่สามารถสร้างแผนที่: ต้องการ folium library")
        return None
        
    try:
        if not route_info or len(path_coords) < 2:
            st.warning("⚠️ ข้อมูลเส้นทางไม่เพียงพอสำหรับสร้างแผนที่")
            return None
        
        # หาจุดกึ่งกลาง
        center_lat = sum([coord[0] for coord in path_coords]) / len(path_coords)
        center_lon = sum([coord[1] for coord in path_coords]) / len(path_coords)
        
        # สร้างแผนที่
        route_map = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=10,
            tiles='OpenStreetMap',
            prefer_canvas=True
        )
        
        # เพิ่ม markers สำหรับแต่ละสถานี
        for info in route_info:
            try:
                if info.get('is_base', False):
                    color = 'green'
                    icon = 'home'
                    popup_text = f"<b>🏢 {info['order']}. {info['station_id']}</b><br><strong>{info['name_th']}</strong><br><i>ฐานปฏิบัติการ</i>"
                else:
                    if info['order'] == 1:
                        color = 'green'
                        icon = 'play'
                    elif info['order'] == len(route_info):
                        color = 'red'
                        icon = 'stop'
                    else:
                        color = 'blue'
                        icon = 'info-sign'
                    popup_text = f"<b>📡 {info['order']}. {info['station_id']}</b><br><strong>{info['name_th']}</strong>"
                
                folium.Marker(
                    [info['lat'], info['lon']],
                    popup=folium.Popup(popup_text, max_width=200),
                    tooltip=f"{info['order']}. {info['station_id']}",
                    icon=folium.Icon(color=color, icon=icon)
                ).add_to(route_map)
            except Exception as marker_error:
                continue  # ข้าม marker ที่มีปัญหา
        
        # เพิ่มเส้นเชื่อมเส้นทาง
        try:
            folium.PolyLine(
                path_coords,
                color='red',
                weight=4,
                opacity=0.8,
                popup=f'เส้นทางรวม: {total_distance:.2f} กม.'
            ).add_to(route_map)
        except Exception as line_error:
            st.warning("⚠️ ไม่สามารถแสดงเส้นเชื่อมได้")
        
        # เพิ่ม distance markers ระหว่างสถานี
        try:
            for i in range(len(path_coords)-1):
                mid_lat = (path_coords[i][0] + path_coords[i+1][0]) / 2
                mid_lon = (path_coords[i][1] + path_coords[i+1][1]) / 2
                distance = geodesic(path_coords[i], path_coords[i+1]).km
                
                folium.Marker(
                    [mid_lat, mid_lon],
                    popup=f"ระยะห่าง: {distance:.1f} กม.",
                    icon=folium.DivIcon(
                        html=f'<div style="background-color: white; border: 2px solid red; border-radius: 5px; padding: 2px 4px; font-size: 10px; font-weight: bold; color: red;">{distance:.1f}กม</div>',
                        icon_size=(50, 20),
                        icon_anchor=(25, 10)
                    )
                ).add_to(route_map)
        except Exception as distance_error:
            pass  # Distance markers ไม่จำเป็น ถ้าเกิดข้อผิดพลาดก็ข้ามไป
        
        return route_map
        
    except Exception as e:
        st.error(f"ข้อผิดพลาดในการสร้างแผนที่เส้นทาง: {str(e)}")
        return None

# ✅ Data Loading Functions
@st.cache_data(ttl=3600)
def load_station_data(file_path: str = 'Latlonstation_config.json') -> pd.DataFrame:
    """โหลดข้อมูลสถานีพร้อม error handling"""
    try:
        if not os.path.exists(file_path):
            st.warning(f"⚠️ ไม่พบไฟล์ {file_path} - ใช้ข้อมูลตัวอย่าง")
            sample_data = {
                'G1001': {'name_th': 'สถานีตัวอย่าง 1', 'lat': 13.7563, 'lon': 100.5018, 'url': ''},
                'G1002': {'name_th': 'สถานีตัวอย่าง 2', 'lat': 13.7263, 'lon': 100.5318, 'url': ''},
                'G1003': {'name_th': 'สถานีตัวอย่าง 3', 'lat': 8.5000, 'lon': 98.5000, 'url': ''}
            }
            return pd.DataFrame.from_dict(sample_data, orient='index').reset_index().rename(columns={'index': 'station_id'})
        
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if not data:
            st.error("❌ ไฟล์ข้อมูลว่างเปล่า")
            return pd.DataFrame()
        
        df = pd.DataFrame.from_dict(data, orient='index')
        df.reset_index(inplace=True)
        df.rename(columns={'index': 'station_id'}, inplace=True)
        
        # ✅ Data validation และ cleaning
        required_columns = ['station_id', 'lat', 'lon']
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            st.error(f"❌ ข้อมูลไม่สมบูรณ์ ขาดคอลัมน์: {missing_columns}")
            return pd.DataFrame()
        
        # Clean และ validate ข้อมูล
        df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
        df['lon'] = pd.to_numeric(df['lon'], errors='coerce')
        df['name_th'] = df.get('name_th', '').fillna('ไม่มีชื่อ')
        df['url'] = df.get('url', '').fillna('')
        
        # ลบแถวที่มีพิกัดไม่ถูกต้อง
        original_count = len(df)
        df = df.dropna(subset=['lat', 'lon'])
        if len(df) < original_count:
            st.info(f"ℹ️ กรองข้อมูลที่ไม่สมบูรณ์: {original_count - len(df)} รายการ")
        
        return df
        
    except json.JSONDecodeError as e:
        st.error(f"❌ ไฟล์ JSON ไม่ถูกต้อง: {str(e)}")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"❌ เกิดข้อผิดพลาดในการโหลดข้อมูล: {str(e)}")
        return pd.DataFrame()

# ✅ Session State Management
def init_session_state():
    """เริ่มต้น session state พร้อม validation"""
    defaults = {
        'selected_stations': [],
        'map_mode': 'select',
        'last_calculation_time': 0,
        'include_base_location': False,
        'last_rerun_time': 0,
        'last_map_click': None,
        'last_map_click_time': 0.0,
        'map_version': 0,
        'pending_station': None,
        'pending_ts': 0.0,
    }
    
    for key, default_value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = default_value
    
    # ✅ จำกัดจำนวนสถานีที่เลือกได้ (ย้อนกลับ: ใช้ 100 ตามการตั้งค่าปัจจุบันของคุณ)
    MAX_STATIONS = 100
    if len(st.session_state.selected_stations) > MAX_STATIONS:
        st.session_state.selected_stations = st.session_state.selected_stations[:MAX_STATIONS]
        st.warning(f"⚠️ จำกัดการเลือกสูงสุด {MAX_STATIONS} สถานี")

def safe_update_session_state(key: str, value, rerun: bool = False):
    """อัปเดต session state อย่างปลอดภัย"""
    try:
        st.session_state[key] = value
        if rerun:
            smart_rerun()
    except Exception as e:
        st.error(f"ข้อผิดพลาดในการอัปเดตสถานะ: {str(e)}")

# ✅ TSP Algorithm
@st.cache_data(ttl=1800)
def calculate_optimal_route(
    stations_data: List[Dict], 
    start_station: str, 
    end_station: str,
    max_stations_exact: int = 10
) -> Tuple[List[str], float]:
    """คำนวณเส้นทางที่เหมาะสมพร้อม performance optimization"""
    
    if len(stations_data) < 2:
        return [], 0.0
    
    try:
        # สร้าง DataFrame
        tsp_df = pd.DataFrame(stations_data)
        
        if tsp_df.empty or 'lat' not in tsp_df.columns or 'lon' not in tsp_df.columns:
            return [], 0.0
        
        # ✅ ตรวจสอบและทำความสะอาดข้อมูลพิกัด
        tsp_df['lat'] = pd.to_numeric(tsp_df['lat'], errors='coerce')
        tsp_df['lon'] = pd.to_numeric(tsp_df['lon'], errors='coerce')
        tsp_df = tsp_df.dropna(subset=['lat', 'lon'])
        
        if len(tsp_df) < 2:
            return [], 0.0
        
        # สร้าง graph
        G = nx.complete_graph(len(tsp_df))
        positions = list(zip(tsp_df['lat'], tsp_df['lon']))
        id_map = dict(zip(range(len(tsp_df)), tsp_df['station_id']))
        reverse_id_map = {v: k for k, v in id_map.items()}
        
        # คำนวณระยะทาง
        for i in G.nodes:
            for j in G.nodes:
                if i != j:
                    try:
                        dist = geodesic(positions[i], positions[j]).km
                        G[i][j]['weight'] = dist
                    except Exception:
                        G[i][j]['weight'] = float('inf')
        
        # ตรวจสอบว่า start และ end station มีอยู่จริง
        if start_station not in reverse_id_map or end_station not in reverse_id_map:
            st.warning(f"⚠️ ไม่พบสถานี {start_station} หรือ {end_station} ในข้อมูล")
            return [], 0.0
        
        start_node = reverse_id_map[start_station]
        end_node = reverse_id_map[end_station]
        nodes = [n for n in G.nodes if n not in [start_node, end_node]]
        
        best_route = None
        min_distance = float('inf')
        
        # เลือกอัลกอริทึมตามจำนวนสถานี
        if len(G.nodes) <= max_stations_exact:
            # Exact algorithm สำหรับสถานีน้อย
            for perm in itertools.permutations(nodes):
                route = [start_node] + list(perm)
                if start_node != end_node:
                    route.append(end_node)
                else:
                    route.append(start_node)
                
                distance = 0
                valid_route = True
                for i in range(len(route)-1):
                    weight = G[route[i]][route[i+1]].get('weight', float('inf'))
                    if weight == float('inf'):
                        valid_route = False
                        break
                    distance += weight
                
                if valid_route and distance < min_distance:
                    min_distance = distance
                    best_route = route
        else:
            # Approximation algorithm สำหรับสถานีมาก
            try:
                cycle = nx.approximation.traveling_salesman_problem(
                    G, weight='weight', cycle=True
                )
                
                # ปรับให้เริ่มต้นและจบที่สถานีที่ต้องการ
                if start_node in cycle:
                    start_idx = cycle.index(start_node)
                    cycle = cycle[start_idx:] + cycle[:start_idx]
                
                if start_node != end_node and end_node in cycle:
                    end_idx = cycle.index(end_node)
                    # สร้างเส้นทางที่เริ่มต้นและจบถูกต้อง
                    route_nodes = [n for n in cycle if n not in [start_node, end_node]]
                    best_route = [start_node] + route_nodes + [end_node]
                else:
                    best_route = cycle
                
                min_distance = sum(G[best_route[i]][best_route[i+1]].get('weight', 0) 
                                 for i in range(len(best_route)-1))
                                 
            except Exception as e:
                st.warning(f"⚠️ ไม่สามารถคำนวณเส้นทางที่เหมาะสม: {str(e)}")
                # Fallback: เส้นทางแบบง่าย
                best_route = [start_node] + nodes + ([end_node] if start_node != end_node else [start_node])
                min_distance = sum(G[best_route[i]][best_route[i+1]].get('weight', 0) 
                                 for i in range(len(best_route)-1))
        
        if best_route is None or min_distance == float('inf'):
            return [], 0.0
        
        ordered_stations = [id_map[i] for i in best_route]
        return ordered_stations, min_distance
        
    except Exception as e:
        st.error(f"ข้อผิดพลาดในการคำนวณเส้นทาง: {str(e)}")
        return [], 0.0

# ✅ Interactive Map Functions
def create_interactive_map(df_filtered: pd.DataFrame, include_base: bool = False):
    """สร้างแผนที่ interactive พร้อม error handling"""
    try:
        import folium
    except ImportError:
        st.error("❌ กรุณาติดตั้ง: pip install folium")
        return None
    
    try:
        # รวมข้อมูลฐานหลัก (ถ้าเลือก)
        display_df = df_filtered.copy()
        if include_base:
            base_df = pd.DataFrame([BASE_LOCATION])
            display_df = pd.concat([display_df, base_df], ignore_index=True)
        
        if display_df.empty:
            st.warning("ไม่มีข้อมูลสถานีสำหรับแสดงบนแผนที่")
            return None
        
        # ตรวจสอบข้อมูลพิกัด
        valid_coords = display_df.dropna(subset=['lat', 'lon'])
        if valid_coords.empty:
            st.warning("ไม่มีข้อมูลพิกัดที่ถูกต้อง")
            return None
        
        # หาจุดกึ่งกลางของแผนที่
        center_lat = valid_coords['lat'].mean()
        center_lon = valid_coords['lon'].mean()
        
        # ตรวจสอบค่า center
        if pd.isna(center_lat) or pd.isna(center_lon):
            center_lat, center_lon = 13.7563, 100.5018  # default: Bangkok
        
        # สร้างแผนที่
        m = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=8,
            tiles='OpenStreetMap',
            prefer_canvas=True
        )
        # ไม่ใช้ MarkerCluster ในเวอร์ชันย้อนกลับ
        
        # เพิ่ม marker สำหรับแต่ละสถานี
        for idx, row in valid_coords.iterrows():
            try:
                station_id = str(row['station_id'])
                name_th = str(row.get('name_th', 'ไม่มีชื่อ'))
                lat = safe_float_conversion(row['lat'])
                lon = safe_float_conversion(row['lon'])
                
                # ตรวจสอบพิกัดที่สมเหตุสมผล
                if (lat == 0.0 and lon == 0.0) or not (5.0 <= lat <= 21.0 and 97.0 <= lon <= 106.0):
                    continue
                
                # เช็คประเภทของสถานี
                is_base_station = station_id == BASE_LOCATION['station_id']
                is_selected = station_id in st.session_state.get('selected_stations', [])
                
                # กำหนดสีและไอคอนตามประเภท
                if is_base_station:
                    color = 'green'
                    icon = 'home'
                    prefix = 'fa'
                elif is_selected:
                    color = 'red'
                    icon = 'star'
                    prefix = 'fa'
                else:
                    color = 'blue'
                    icon = 'tint'
                    prefix = 'fa'
                # ... หลังจากกำหนด is_base_station, is_selected, color, icon แล้ว
                dnm_val = None
                try:
                    row_info = df_filtered[df_filtered['station_id'] == station_id]
                    if not row_info.empty and 'days_not_maintained' in row_info.columns:
                        dnm_val = row_info.iloc[0].get('days_not_maintained')
                except:
                    pass

                days_txt = (
                    f"<div style='margin-top:4px;'>🛠️ ไม่ได้บำรุงมา <b>{int(dnm_val)}</b> วัน</div>"
                    if pd.notna(dnm_val) else ""
                )
                # สร้าง popup text
                if is_base_station:
                    popup_text = f"""
                    <div style="min-width: 220px; text-align: center;">
                    <b style="color: green; font-size: 14px;">🏢 {name_th}</b><br>
                    <strong>รหัส: {station_id}</strong><br>
                    ตำแหน่ง: {lat:.4f}, {lon:.4f}<br>
                    <div style="margin: 5px 0; padding: 5px; background-color: #e8f5e8; border-radius: 3px; border: 1px solid #4caf50;">
                    <strong>🎯 ฐานปฏิบัติการหลัก</strong>
                    </div>
                    <small style="color: #666;">
                    <a href="{row.get('url', '#')}" target="_blank">📍 ดูใน Google Maps</a>
                    </small>
                    </div>
                    """
                else:
                    popup_text = f"""
                    <div style="min-width: 200px; text-align: center;">
                    <b style="color: {'red' if is_selected else 'blue'};">{station_id}</b><br>
                    <strong>{name_th}</strong><br>
                    ตำแหน่ง: {lat:.4f}, {lon:.4f}<br>
                    {days_txt}
                    <div style="margin: 5px 0; padding: 5px; background-color: {'#ffebee' if is_selected else '#e3f2fd'}; border-radius: 3px;">
                    สถานะ: {'✅ เลือกแล้ว' if is_selected else '⚪ ยังไม่เลือก'}
                    </div>
                    <small style="color: #666;">💡 คลิกที่ marker เพื่อ{'ยกเลิก' if is_selected else 'เลือก'}</small>
                    </div>
                    """
                def color_by_days(d):
                    if d is None or not pd.notna(d): 
                        return "blue"
                    v = int(d)
                    if v > 60: 
                        return "red"       # > 60 วัน = แดง
                    if v > 31: 
                        return "orange"    # 32–60 วัน = เหลือง
                    return "green"         # ≤ 31 วัน = เขียว

                if not is_base_station:
                    color = color_by_days(dnm_val)
                # ทำ tooltip แสดงรหัสถังตลอดเวลา
                label = f"{station_id} | {int(dnm_val)} วัน" if pd.notna(dnm_val) else station_id  # ถ้าจะใส่วันด้วย: f"{station_id} | {int(dnm_val)} วัน" if pd.notna(dnm_val) else station_id
                tooltip = folium.Tooltip(label, permanent=true, direction="top", sticky=False)   
                # เพิ่ม marker
                folium.Marker(
                    [lat, lon],
                    popup=folium.Popup(popup_text, max_width=250),
                    tooltip=tooltip,   # ← เปลี่ยนตรงนี้
                    icon=folium.Icon(color=color, icon=icon, prefix=prefix)
                ).add_to(m)
                
            except Exception as marker_error:
                continue  # ข้าม marker ที่มีปัญหา
        
        return m
        
    except Exception as e:
        st.error(f"ข้อผิดพลาดในการสร้างแผนที่: {str(e)}")
        return None

# ✅ Main Application
def main():
    """แอปพลิเคชันหลัก"""
    try:
        init_session_state()
        
        st.title("📡 Rain Gauge Station Viewer & Interactive Route Planner")
        
        # โหลดข้อมูล
        with st.spinner("กำลังโหลดข้อมูลสถานี..."):
            df = load_station_data()
            try:
                sheet_df = load_sheet_days()
                if not sheet_df.empty:
                    df = df.merge(sheet_df, on="station_id", how="left")
                else:
                    st.warning("ไม่พบข้อมูลจาก Google Sheet")
            except Exception as e:
                st.warning(f"โหลดชีตไม่ได้: {e}")
        
        if df.empty:
            st.error("❌ ไม่สามารถโหลดข้อมูลสถานีได้")
            st.stop()
        
        # ฟิลเตอร์ค้นหา
        search_term = st.text_input("🔍 ค้นหาด้วยรหัสหรือชื่อสถานี")
        
        # ปรับปรุง search logic
        if search_term:
            search_term = search_term.strip()
            if search_term:
                try:
                    df_filtered = df[
                        df['station_id'].astype(str).str.contains(search_term, case=False, na=False) |
                        df['name_th'].astype(str).str.contains(search_term, case=False, na=False)
                    ]
                except Exception as search_error:
                    st.warning(f"⚠️ ข้อผิดพลาดในการค้นหา: {str(search_error)}")
                    df_filtered = df
            else:
                df_filtered = df
        else:
            df_filtered = df
        
        # ฟิลเตอร์พิกัด
        st.sidebar.header("🔧 ฟิลเตอร์สถานี")
        only_with_coords = st.sidebar.checkbox("แสดงเฉพาะสถานีที่มีพิกัดครบ", value=True)
        
        if only_with_coords:
            df_filtered = df_filtered.dropna(subset=['lat', 'lon'])
        
        # แสดงสถิติ
        st.sidebar.metric("จำนวนสถานีทั้งหมด", len(df))
        st.sidebar.metric("สถานีที่แสดง", len(df_filtered))
        st.sidebar.metric("สถานีที่เลือก", len(st.session_state.get('selected_stations', [])))
        
        # แสดงตารางข้อมูล
        with st.expander("📋 รายชื่อสถานี", expanded=False):
            if not df_filtered.empty:
                display_columns = ['station_id', 'name_th', 'lat', 'lon']
                available_columns = [col for col in display_columns if col in df_filtered.columns]
                st.dataframe(df_filtered[available_columns], use_container_width=True)
            else:
                st.warning("ไม่มีข้อมูลที่ตรงกับเงื่อนไขการค้นหา")
        
        # Interactive Map Section
        st.subheader("🗺️ แผนที่ Interactive สำหรับเลือกสถานี")
        
        # เพิ่มตัวเลือกรวมฐานหลัก
        col_base, col_info = st.columns([1, 2])
        with col_base:
            # ✅ แก้ไข: ใช้ key เฉพาะและไม่ rerun ใน callback
            include_base = st.checkbox(
                "🏢 รวมตำแหน่งฐานปฏิบัติการ",
                value=st.session_state.get('include_base_location', False),
                help="เพิ่มตำแหน่งศูนย์ปฏิบัติการหลัก (8.8485°N, 98.8094°E)",
                key="include_base_checkbox"
            )
            
            # ✅ แก้ไข: ไม่ rerun ทันที แต่เก็บสถานะไว้
            if include_base != st.session_state.get('include_base_location', False):
                st.session_state.include_base_location = include_base
                cleanup_selected_stations()
                # ไม่ rerun ที่นี่ - ให้ Streamlit จัดการเอง
        
        with col_info:
            # ✅ ใช้ค่าจาก session state แทนการใช้ตัวแปร include_base
            if st.session_state.get('include_base_location', False):
                st.info(f"📍 **{BASE_LOCATION['name_th']}**  \n📌 {BASE_LOCATION['lat']:.4f}, {BASE_LOCATION['lon']:.4f}")
        
        # Control buttons
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            if st.button("🎯 โหมดเลือกสถานี", help="คลิกที่ marker เพื่อเลือกสถานี"):
                safe_update_session_state('map_mode', 'select')
        
        with col2:
            if st.button("🛣️ ยังไม่เปิดให้ใช้งาน", help="แสดงเส้นทางบนแผนที่"):
                safe_update_session_state('map_mode', 'route')
        
        with col3:
            if st.button("🗑️ ล้างการเลือก", help="ลบการเลือกทั้งหมด"):
                if st.session_state.get('selected_stations', []):
                    count = len(st.session_state.selected_stations)
                    safe_update_session_state('selected_stations', [])
                    st.success(f"ล้างการเลือก {count} สถานีแล้ว")
                    # รีเซ็ตสถานะคลิกล่าสุดด้วย เพื่อไม่ให้ debounce บล็อกคลิกถัดไป
                    st.session_state.last_map_click = None
                    st.session_state.last_map_click_time = 0.0
                    st.session_state.map_version = st.session_state.get('map_version', 0) + 1
        
        with col4:
            selected_count = len(st.session_state.get('selected_stations', []))
            MAX_STATIONS = 100
            st.metric(
                "เลือกแล้ว", 
                f"{selected_count}/{MAX_STATIONS} สถานี", 
                delta=f"{MAX_STATIONS-selected_count} เหลือ" if selected_count < MAX_STATIONS else "เต็มแล้ว"
            )
        
        # แสดงแผนที่
        if not df_filtered.empty or include_base:
            # บังคับรีสร้าง key ของแผนที่เมื่อมีการเลือก/ยกเลิก เพื่อให้ event handler ใหม่ทำงานเสมอ
            map_key = f"main_map_{st.session_state.get('map_version', 0)}"
            map_obj = create_interactive_map(df_filtered, include_base)
            if map_obj:
                # แสดงคำแนะนำการใช้งาน
                map_mode = st.session_state.get('map_mode', 'select')
                if map_mode == 'select':
                    st.info("🎯 **โหมดเลือกสถานี**: คลิกที่ marker บนแผนที่เพื่อเลือก/ยกเลิกการเลือกสถานี")
                else:
                    st.info("🛣️ **โหมดแสดงเส้นทาง**: แสดงเส้นทางที่วางแผนไว้")
                
                try:
                    from streamlit_folium import st_folium
                    
                    map_data = st_folium(
                        map_obj, 
                        width=700, 
                        height=500,
                        returned_objects=["last_clicked", "last_object_clicked"],
                        key=map_key
                    )
                    
                    # จัดการการคลิกบนแผนที่เพื่อเลือกสถานี
                    clicked_event = None
                    if map_data:
                        # ใช้ last_object_clicked เป็นหลัก (เสถียรกว่าในหลายเวอร์ชัน), ถ้าไม่มีค่อย fallback เป็น last_clicked
                        if map_data.get('last_object_clicked') and map_mode == 'select':
                            clicked_event = map_data.get('last_object_clicked')
                        elif map_data.get('last_clicked') and map_mode == 'select':
                            clicked_event = map_data.get('last_clicked')
                    
                    if clicked_event and clicked_event.get('lat') is not None:
                        clicked_lat = clicked_event['lat']
                        clicked_lng = clicked_event['lng']
                        # ป้องกันการประมวลผลคลิกเดิมซ้ำ ๆ ภายในช่วงเวลาสั้น ๆ
                        current_click = (round(clicked_lat, 5), round(clicked_lng, 5))
                        last_click = st.session_state.get('last_map_click')
                        last_click_time = st.session_state.get('last_map_click_time', 0.0)
                        now_ts = time.time()
                        # ถ้าคลิกพิกัดเดิมเป๊ะในช่วงสั้น ๆ ให้ข้าม เพื่อลดการกระตุ้นซ้ำ
                        is_duplicate_very_recent = (last_click == current_click) and (now_ts - last_click_time < 0.2)
                        if not is_duplicate_very_recent:
                            # หาสถานีที่ใกล้ที่สุด
                            closest_station = find_nearest_station_optimized(
                                clicked_lat, clicked_lng, df_filtered, include_base, 500
                            )
                            if not closest_station:
                                # ขยายรัศมีค้นหาเป็น 1 กม. ถ้าไม่เจอใน 500 ม.
                                closest_station = find_nearest_station_optimized(
                                    clicked_lat, clicked_lng, df_filtered, include_base, 1000
                                )
                            if not closest_station:
                                # ลองครั้งสุดท้ายที่ 2 กม. สำหรับการคลิกใกล้แต่ไม่โดนเป๊ะ
                                closest_station = find_nearest_station_optimized(
                                    clicked_lat, clicked_lng, df_filtered, include_base, 2000
                                )
                            
                            # เลือก/ยกเลิกการเลือกสถานี
                            if closest_station:
                                try:
                                    current_stations = st.session_state.get('selected_stations', [])
                                    if closest_station in current_stations:
                                        current_stations.remove(closest_station)
                                        st.success(f"❌ ยกเลิกการเลือก: {closest_station}")
                                    else:
                                        MAX_STATIONS = 100
                                        if len(current_stations) < MAX_STATIONS:
                                            current_stations.append(closest_station)
                                            st.success(f"✅ เลือก: {closest_station}")
                                        else:
                                            st.warning(f"⚠️ เลือกได้สูงสุด {MAX_STATIONS} สถานี")
                                    st.session_state.selected_stations = current_stations

                                    # อัปเดตสถานะคลิก
                                    st.session_state.last_map_click = current_click
                                    st.session_state.last_map_click_time = now_ts
                                    st.session_state.map_version = st.session_state.get('map_version', 0) + 1

                                    # ให้รีเฟรช map เพื่ออัปเดตสี marker
                                    smart_rerun()

                                except Exception as selection_error:
                                    st.error(f"ข้อผิดพลาดในการจัดการการเลือก: {str(selection_error)}")
                            else:
                                st.caption("คลิกใกล้ marker สถานีเพื่อเลือก (≤ 1–2 km หากซูมไกล)")
                            
                except ImportError:
                    st.error("❌ กรุณาติดตั้ง streamlit-folium: pip install streamlit-folium")
                    st.code("pip install streamlit-folium", language="bash")
                    st.stop()
                except Exception as map_error:
                    st.error(f"ข้อผิดพลาดในการแสดงแผนที่: {str(map_error)}")
                    st.info("💡 กรุณารีเฟรชหน้าแล้วลองใหม่")
        else:
            st.warning("ไม่มีข้อมูลสถานีสำหรับแสดงบนแผนที่")
        
        # ส่วนจัดการสถานีที่เลือก
        st.subheader("📝 จัดการสถานีสำหรับวางแผนเส้นทาง")
        
        selected_stations = st.session_state.get('selected_stations', [])
        
        # แสดงสถานีที่เลือกแล้ว
        if selected_stations:
            st.write("**สถานีที่เลือกแล้ว:**")
            
            # แสดงรายการที่เลือกแบบ grid
            cols = st.columns(min(len(selected_stations), 4))
            stations_to_remove = []
            
            for i, station_id in enumerate(selected_stations):
                with cols[i % 4]:
                    if station_id == BASE_LOCATION['station_id']:
                        station_name = BASE_LOCATION['name_th']
                        icon = "🏢"
                    else:
                        station_name = safe_get_station_name(df_filtered, station_id)
                        icon = "📡"
                    
                    if st.button(f"❌ {icon} {station_id}", key=f"remove_{station_id}_{i}"):
                        stations_to_remove.append(station_id)
                    st.caption(station_name)
            
            # ลบสถานีที่เลือก
            for station_id in stations_to_remove:
                if station_id in selected_stations:
                    selected_stations.remove(station_id)
                    st.session_state.selected_stations = selected_stations
        
        # ตัวเลือกเพิ่มสถานี
        st.write("**เพิ่มสถานีใหม่:**")
        
        # สร้างรายการสถานีที่ยังไม่ได้เลือก
        available_stations = df_filtered[
            ~df_filtered['station_id'].isin(selected_stations)
        ] if not df_filtered.empty else pd.DataFrame()
        
        if not available_stations.empty:
            def format_station_option(station_id):
                # ใช้ df เต็มเพื่อดึงชื่อแม้จะถูกฟิลเตอร์
                station_name = safe_get_station_name(df, station_id)
                return f"📡 {station_id} - {station_name}"
            
            MAX_STATIONS = 100
            selected_new_stations = st.multiselect(
                "เลือกสถานีที่ต้องการเพิ่ม:",
                options=available_stations['station_id'].tolist(),
                format_func=format_station_option,
                max_selections=max(0, MAX_STATIONS - len(selected_stations)),
                help=f"เลือกได้อีก {max(0, MAX_STATIONS - len(selected_stations))} สถานี"
            )
            
            if st.button("➕ เพิ่มสถานีที่เลือก") and selected_new_stations:
                try:
                    current_stations = set(selected_stations)
                    new_stations = current_stations.union(set(selected_new_stations))
                    st.session_state.selected_stations = list(new_stations)
                    st.success(f"เพิ่มสถานีสำเร็จ {len(selected_new_stations)} แห่ง")
                except Exception as add_error:
                    st.error(f"ข้อผิดพลาดในการเพิ่มสถานี: {str(add_error)}")
        else:
            st.info("เลือกสถานีครบแล้ว หรือไม่มีสถานีให้เลือกเพิ่ม")
        
        # วางแผนเส้นทาง
        st.subheader("🛣️ วางแผนเส้นทางการเดินทาง")
        
        if len(selected_stations) >= 2:
            # เตรียมตัวเลือกสำหรับ selectbox
            station_options = []
            station_name_map = {}
            
            for station_id in selected_stations:
                if station_id == BASE_LOCATION['station_id']:
                    display_name = f"🏢 {station_id} - {BASE_LOCATION['name_th']}"
                else:
                    # ใช้ df เต็มเพื่อให้ได้ชื่อเสมอ
                    station_name = safe_get_station_name(df, station_id)
                    display_name = f"📡 {station_id} - {station_name}"
                
                station_options.append(display_name)
                station_name_map[display_name] = station_id
            
            col1, col2 = st.columns(2)
            with col1:
                # ให้ฐานหลักเป็น default ถ้ามี
                default_start = 0
                if BASE_LOCATION['station_id'] in selected_stations:
                    for i, option in enumerate(station_options):
                        if BASE_LOCATION['station_id'] in option:
                            default_start = i
                            break
                
                start_display = st.selectbox(
                    "จุดเริ่มต้น:",
                    station_options,
                    key="start_station_display",
                    index=default_start
                )
                start_station = station_name_map[start_display]
            
            with col2:
                # ให้ฐานหลักเป็น default สำหรับ end ถ้ามี
                default_end = len(station_options) - 1
                if BASE_LOCATION['station_id'] in selected_stations:
                    for i, option in enumerate(station_options):
                        if BASE_LOCATION['station_id'] in option:
                            default_end = i
                            break
                
                end_display = st.selectbox(
                    "จุดสิ้นสุด:",
                    station_options,
                    key="end_station_display",
                    index=default_end
                )
                end_station = station_name_map[end_display]
            
            # ป้องกัน rate limiting
            current_time = time.time()
            last_calc_time = st.session_state.get('last_calculation_time', 0)
            
            if st.button("🧮 คำนวณเส้นทางที่เหมาะสม"):
                if current_time - last_calc_time < 2:
                    st.warning("⏰ กรุณารอสักครู่ก่อนคำนวณใหม่")
                else:
                    st.session_state.last_calculation_time = current_time
                    
                    with st.spinner("กำลังคำนวณเส้นทางที่เหมาะสม..."):
                        try:
                            # เตรียมข้อมูลสถานี
                            stations_data = []
                            
                            for station_id in selected_stations:
                                if station_id == BASE_LOCATION['station_id']:
                                    stations_data.append(BASE_LOCATION.copy())
                                else:
                                    station_info = df_filtered[df_filtered['station_id'] == station_id]
                                    if not station_info.empty:
                                        station_dict = station_info.iloc[0].to_dict()
                                        # ให้แน่ใจว่ามี name_th
                                        if 'name_th' not in station_dict or pd.isna(station_dict['name_th']):
                                            station_dict['name_th'] = 'ไม่ทราบชื่อ'
                                        stations_data.append(station_dict)
                            
                            # คำนวณเส้นทาง
                            ordered_stations, min_distance = calculate_optimal_route(
                                stations_data, start_station, end_station
                            )
                            
                            if ordered_stations and min_distance > 0:
                                # เตรียมเส้นทางสำหรับแสดงผลและเก็บลง session
                                route_info = []
                                path_coords = []
                                for idx, station_id in enumerate(ordered_stations):
                                    if station_id == BASE_LOCATION['station_id']:
                                        station_data = BASE_LOCATION
                                        is_base = True
                                    else:
                                        station_info = df[df['station_id'] == station_id]
                                        if not station_info.empty:
                                            station_data = station_info.iloc[0].to_dict()
                                            if 'name_th' not in station_data or pd.isna(station_data['name_th']):
                                                station_data['name_th'] = 'ไม่ทราบชื่อ'
                                            is_base = False
                                        else:
                                            continue
                                    try:
                                        lat = safe_float_conversion(station_data['lat'])
                                        lon = safe_float_conversion(station_data['lon'])
                                        path_coords.append([lat, lon])
                                        route_info.append({
                                            'station_id': station_id,
                                            'name_th': str(station_data['name_th']),
                                            'lat': lat,
                                            'lon': lon,
                                            'order': idx + 1,
                                            'is_base': is_base
                                        })
                                    except Exception:
                                        continue
                                st.session_state.route_result = {
                                    'ordered_stations': ordered_stations,
                                    'min_distance': float(min_distance),
                                    'route_info': route_info,
                                    'path_coords': path_coords
                                }
                                st.success("✅ บันทึกผลลัพธ์เส้นทางแล้ว")
                            else:
                                st.error("❌ ไม่สามารถคำนวณเส้นทางได้ กรุณาตรวจสอบข้อมูลสถานี")
                                
                        except Exception as calc_error:
                            st.error(f"ข้อผิดพลาดในการคำนวณ: {str(calc_error)}")
                            st.info("💡 กรุณาลองใหม่หรือติดต่อผู้ดูแลระบบ")
        
        elif len(selected_stations) == 1:
            st.warning("⚠️ กรุณาเลือกอย่างน้อย 2 สถานีเพื่อวางแผนเส้นทาง")
        else:
            st.info("ℹ️ เริ่มต้นโดยการเลือกสถานีจากแผนที่หรือรายการด้านบน")
        
        # Planning Section (รักษาส่วนเดิมไว้)
        st.subheader("📆 วางแผนตรวจสอบถังน้ำฝน")
        
        with st.expander("📋 คำนวณแผนการตรวจสอบ", expanded=False):
            total_stations = len(df_filtered)
            
            col1, col2, col3 = st.columns(3)
            with col1:
                max_days_available = st.number_input("ระยะเวลาทั้งหมด (วัน):", min_value=1, value=30)
            with col2:
                avg_stations_per_day = st.number_input("สถานีที่ตรวจได้ต่อวัน:", min_value=1, value=7)
            with col3:
                target_stations = st.number_input("จำนวนที่ต้องการตรวจ:", min_value=1, value=min(total_stations, 50))
            
            # คำนวณ
            min_required_days = math.ceil(target_stations / avg_stations_per_day)
            total_capacity = max_days_available * avg_stations_per_day
            suggested_per_day = math.ceil(target_stations / max_days_available)
            
            st.info(f"🎯 ต้องการตรวจ {target_stations} ถัง ภายใน {max_days_available} วัน")
            
            if total_capacity < target_stations:
                st.error(f"❌ ตรวจได้สูงสุด {total_capacity} ถัง ซึ่งไม่เพียงพอ")
                st.warning(f"🔄 ควรตรวจอย่างน้อยวันละ {suggested_per_day} ถัง")
            else:
                st.success(f"✅ ตรวจได้ใน {min_required_days} วัน หากวันละ {avg_stations_per_day} จุด")

        # แสดงผลลัพธ์เส้นทางล่าสุดจาก session ถ้ามี (กันกรณีแสดงแล้วหาย)
        if 'route_result' in st.session_state:
            res = st.session_state['route_result']
            ordered_stations = res.get('ordered_stations') or []
            min_distance = res.get('min_distance') or 0.0
            route_info = res.get('route_info') or []
            path_coords = res.get('path_coords') or []

            if ordered_stations and path_coords:
                st.subheader("📍 ผลลัพธ์เส้นทางล่าสุด")
                st.success(f"📏 ระยะทางรวม: {min_distance:.2f} กิโลเมตร")
                for info in route_info:
                    icon = "🏢" if info.get('is_base') else "📡"
                    st.write(f"{info['order']}. {icon} **{info['station_id']}** - {info['name_th']}")

                if len(path_coords) >= 2:
                    st.markdown("**🧭 เปิดใน Google Maps:**")
                    try:
                        maps_url = "https://www.google.com/maps/dir/" + "/".join([
                            f"{coord[0]},{coord[1]}" for coord in path_coords
                        ])
                        st.markdown(f"[📱 เปิดเส้นทางใน Google Maps]({maps_url})")
                    except Exception:
                        pass

                    st.subheader("🗺️ แผนที่เส้นทางการเดินทาง (ล่าสุด)")
                    try:
                        route_map = create_route_map(route_info, path_coords, min_distance)
                        if route_map:
                            from streamlit_folium import st_folium
                            st_folium(route_map, width=700, height=500, key="route_map_latest")
                    except Exception:
                        pass
        
    except Exception as main_error:
        st.error(f"ข้อผิดพลาดร้ายแรงในแอปพลิเคชัน: {str(main_error)}")
        st.info("💡 กรุณารีเฟรชหน้าเว็บแล้วลองใหม่")
        
        if st.button("🔄 รีเซ็ตแอพพลิเคชัน"):
            # ล้าง session state ทั้งหมด
            for key in list(st.session_state.keys()):
                try:
                    del st.session_state[key]
                except:
                    pass
            # ไม่ต้องบังคับ rerun ปล่อยให้ปุ่มเป็นตัวกระตุ้นเอง

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        st.error(f"ข้อผิดพลาดในการเริ่มต้นแอพพลิเคชัน: {str(e)}")
        st.code("""
        # แก้ไขปัญหา:
        1. ตรวจสอบการติดตั้ง libraries
        2. รีสตาร์ทแอพพลิเคชัน
        3. ตรวจสอบไฟล์ข้อมูล
        """)
        
        if st.button("📥 ดาวน์โหลด Requirements"):
            requirements = """
streamlit>=1.28.0
pandas>=1.5.0
geopy>=2.3.0
networkx>=3.0
folium>=0.14.0
streamlit-folium>=0.13.0
            """.strip()
            st.download_button(
                "💾 ดาวน์โหลด requirements.txt",
                requirements,
                "requirements.txt",
                "text/plain"

            )


















