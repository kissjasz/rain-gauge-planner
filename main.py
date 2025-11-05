# main.py - Enhanced version with status detection (All errors fixed)
import re
import json
import csv
import html as _html
from datetime import datetime, timedelta, timezone
import requests
from bs4 import BeautifulSoup
import time


# --- retry helper for idempotent HTTP requests ---
def request_with_retry(session: requests.Session, method: str, url: str,
                       *, max_attempts: int = 7,
                       base_sleep: float = 0.8,
                       timeout: float = 60,
                       retry_http_status=(500, 502, 503, 504),
                       debug: bool = False,
                       **kwargs) -> requests.Response:
    """
    Retry on server errors, timeouts, and transient network faults.
    Exponential backoff with jitter. Only for idempotent requests.
    """
    attempt = 0
    last_err = None
    while attempt < max_attempts:
        attempt += 1
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code in retry_http_status:
                if debug:
                    print(f"[DEBUG] {url} -> HTTP {resp.status_code} on attempt {attempt}/{max_attempts}")
                raise requests.exceptions.HTTPError(f"{resp.status_code} Server Error", response=resp)
            return resp
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.HTTPError) as e:
            last_err = e
            if attempt >= max_attempts:
                break
            sleep_s = base_sleep * (2 ** (attempt - 1)) + random.random() * base_sleep
            if debug:
                print(f"[DEBUG] Retry {attempt}/{max_attempts} after error: {e}. Sleep {sleep_s:.1f}s")
            time.sleep(sleep_s)
    if last_err:
        raise last_err
    raise RuntimeError("request_with_retry failed without exception")

import random
from typing import Optional

LOGIN_URL = "http://eecmobile1.fortiddns.com/eec/Login.aspx"
MAP_URL = "http://eecmobile1.fortiddns.com/eec/Raingauge_Monitor_Map.aspx"
SUMMARY_URL = "http://eecmobile1.fortiddns.com/eec/Raingauge_Summary_Station.aspx"
ALL_LATEST_URL = "http://eecmobile1.fortiddns.com/eec/Raingauge_All_Lastest.aspx"

# ---------------- Login ----------------
def _inputs(html):
    soup = BeautifulSoup(html, "html.parser")
    return {i.get("name"): i.get("value", "") for i in soup.find_all("input") if i.get("name")}

def login(user, password, debug=False):
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (EECLoginBot/1.0)"})
    if debug:
        print("[DEBUG] GET", LOGIN_URL)
    r = s.get(LOGIN_URL, timeout=60)
    r.raise_for_status()
    data = _inputs(r.text)
    data.update({"tb_user": user, "tb_password": password})
    if debug:
        print("[DEBUG] POST", LOGIN_URL)
    r2 = s.post(LOGIN_URL, data=data, timeout=60, allow_redirects=True)
    if "Default.aspx" in r2.url or "logout" in r2.text.lower():
        if debug:
            print("[DEBUG] Login OK")
        return s
    raise RuntimeError("login failed")

# ---------------- Parse SetMap ----------------
def _tokenize_args(s):
    args = []
    cur = []
    i = 0
    L = len(s)
    state = None
    brace_depth = 0
    
    while i < L:
        c = s[i]
        if state is None:
            if c in "\"'":
                state = c
            elif c == '{':
                state = '{'
                brace_depth = 1
            elif c == ',':
                token = ''.join(cur).strip()
                if token:
                    args.append(token)
                cur = []
                i += 1
                continue
            cur.append(c)
        elif state in ("'", '"'):
            cur.append(c)
            if c == state and (i == 0 or s[i-1] != '\\'):
                state = None
        else:
            cur.append(c)
            if c == '{':
                brace_depth += 1
            elif c == '}':
                brace_depth -= 1
            if brace_depth == 0:
                state = None
        i += 1
    
    token = ''.join(cur).strip()
    if token:
        args.append(token)
    return args

def _clean_str(tok):
    tok = tok.strip()
    if (tok.startswith("'") and tok.endswith("'")) or (tok.startswith('"') and tok.endswith('"')):
        return tok[1:-1].replace("\\'", "'").replace('\\"', '"')
    return tok

def _try_num(tok):
    try:
        if str(tok).isdigit():
            return int(tok)
        else:
            return float(tok)
    except:
        return tok

def _parse_options(tok):
    tok = tok.strip()
    if not tok.startswith('{') or not tok.endswith('}'):
        return tok
    body = tok[1:-1].strip()
    d = {}
    for part in re.split(r',(?=(?:[^:]*:[^:]*$)|(?:[^,]*$))', body):
        if ':' not in part:
            continue
        k, v = part.split(':', 1)
        d[k.strip().strip("'\"")] = _try_num(v.strip())
    return d

def _parse_info_html(info_html):
    if not info_html:
        return {}
    
    s = _html.unescape(info_html)
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    
    def find(k):
        for ln in lines:
            m = re.search(rf'{k}\s*[:]\s*(.+)', ln, flags=re.I)
            if m:
                return m.group(1).strip()
        return None
    
    out = {
        "code": find("Code"),
        "rain": find("Rain"),
        "date": find("Date")
    }
    
    def fnum(v):
        if not v:
            return None
        m = re.search(r'([+-]?\d+(\.\d+)?)', v)
        return float(m.group(1)) if m else None
    
    out["temperature_c"] = fnum(find("Temperature") or find("Temp"))
    out["humidity_pct"] = fnum(find("Humidity"))
    out["battery_v"] = fnum(find("Battery"))
    out["solar_volt_v"] = fnum(find("Solar Panels Voltages") or find("Solar"))
    out["status_text"] = find("Status")
    
    return out

def parse_status_from_icon(icon_data):
    if not icon_data:
        return "UNKNOWN"
    icon_str = str(icon_data).lower()

    # ไอคอนระดับฝน ไม่ใช่สถานะ
    if re.search(r'raingauge[_-]\d+(\.png)?', icon_str):
        return "UNKNOWN"

    # ห้ามอนุมานจากสีสำหรับไอคอนชุดนี้อีกต่อไป
    # ที่เหลือคง mapping จากชื่อไฟล์สถานะจริงเท่านั้น
    if "online" in icon_str or "green" in icon_str or "_1" in icon_str:
        return "ONLINE"
    if "offline" in icon_str or "red" in icon_str or "_0" in icon_str:
        return "OFFLINE"
    if "timeout" in icon_str or "yellow" in icon_str or "orange" in icon_str:
        return "TIMEOUT"
    if "disconnect" in icon_str or "grey" in icon_str or "gray" in icon_str:
        return "DISCONNECT"
    if "repair" in icon_str or "maintenance" in icon_str:
        return "REPAIR"
    return "UNKNOWN"

def fetch_all_stations_status(session, debug=False):
    """ดึงสถานะทั้งหมดจาก Raingauge_All_Lastest.aspx"""
    try:
        if debug:
            print(f"[DEBUG] Fetching all stations status from {ALL_LATEST_URL}")
        
        response = session.get(ALL_LATEST_URL, timeout=60)
        response.raise_for_status()
        
        if debug:
            with open("debug_all_latest.html", "w", encoding="utf-8") as f:
                f.write(response.text)
            print("[DEBUG] ✓ Saved HTML to debug_all_latest.html")
        
        soup = BeautifulSoup(response.text, "html.parser")
        status_dict = {}
        
        panel_body = soup.find("div", class_="panel-body")
        if not panel_body:
            if debug:
                print("[DEBUG] panel-body not found")
            return status_dict
        
        table = panel_body.find("table")
        if not table:
            if debug:
                print("[DEBUG] table not found in panel-body")
            return status_dict
        
        rows = table.find_all("tr")
        if debug:
            print(f"[DEBUG] Found {len(rows)} rows in table")
        
        for row_idx, row in enumerate(rows):
            if row_idx == 0:
                continue
            
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            
            station_code = None
            for cell in cells[:3]:
                text = cell.get_text(strip=True)
                if re.match(r'^G\d+$', text):
                    station_code = text
                    break
            
            if not station_code:
                continue
            
            status_img = row.find("img", id=re.compile(r'.*Img_Status.*'))
            status_src = None
            status_alt = None
            
            if status_img:
                status_src = status_img.get("src", "")
                status_alt = status_img.get("alt", "")
            
            status = parse_status_from_image(status_src, status_alt)
            
            status_info = {
                "status": status,
                "status_src": status_src,
                "status_alt": status_alt,
                "row_data": [cell.get_text(strip=True) for cell in cells[:10]]
            }
            
            status_dict[station_code] = status_info
            
            if debug and row_idx <= 3:
                print(f"[DEBUG] {station_code}: status={status} src={status_src}")
        
        if debug:
            print(f"[DEBUG] ✓ Parsed {len(status_dict)} stations status")
        
        return status_dict
        
    except Exception as e:
        if debug:
            print(f"[DEBUG] Error fetching all stations status: {e}")
            import traceback
            traceback.print_exc()
        return {}

def parse_status_from_image(src, alt):
    """Parse สถานะจาก image src หรือ alt text"""
    if not src and not alt:
        return "UNKNOWN"
    
    combined = f"{src} {alt}".lower()
    
    if "online" in combined or "green" in combined or "normal" in combined:
        return "ONLINE"
    elif "offline" in combined or "red" in combined:
        return "OFFLINE"
    elif "timeout" in combined or "yellow" in combined or "warning" in combined:
        return "TIMEOUT"
    elif "disconnect" in combined or "grey" in combined or "gray" in combined:
        return "DISCONNECT"
    elif "repair" in combined or "maintenance" in combined:
        return "REPAIR"
    
    match = re.search(r'status[_-](\w+)', combined, re.I)
    if match:
        status_text = match.group(1).upper()
        if status_text in ["ONLINE", "OFFLINE", "TIMEOUT", "DISCONNECT", "REPAIR"]:
            return status_text
    
    return "UNKNOWN"

def fetch_station_status_api(session, station_id, debug=False):
    """เรียก API Summary Station เพื่อดึงข้อมูลรายละเอียด"""
    if not station_id:
        return None
    
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%d/%m/%Y")
    time_str = now_utc.strftime("%H:%M")
    
    api_url = f"{SUMMARY_URL}?id={station_id}&d={date_str}&t={time_str}"
    
    try:
        if debug:
            print(f"[DEBUG] API: {api_url}")
        
        response = session.get(api_url, timeout=60)
        response.raise_for_status()
        content = response.text
        
        if debug:
            print(f"[DEBUG] Response length: {len(content)} bytes")
        
        try:
            data = response.json()
            return {
                "status": data.get("status"),
                "raw_data": data
            }
        except:
            pass
        
        soup = BeautifulSoup(content, "html.parser")
        
        status_patterns = {
            'online': r'online|connected|normal|active',
            'offline': r'offline|disconnected',
            'timeout': r'timeout|warning|delayed',
            'disconnect': r'disconnect',
            'repair': r'repair|maintenance'
        }
        
        content_lower = content.lower()
        for status, pattern in status_patterns.items():
            if re.search(pattern, content_lower):
                if debug:
                    print(f"[DEBUG] Found status keyword: {status}")
                return {
                    "status": status.upper(),
                    "html_length": len(content)
                }
        
        if len(content) > 500:
            return {
                "status": "ONLINE",
                "html_length": len(content),
                "note": "Assumed from valid response"
            }
        
        return {
            "status": "UNKNOWN",
            "html_length": len(content)
        }
        
    except requests.exceptions.HTTPError as e:
        if debug:
            print(f"[DEBUG] HTTP Error: {e}")
        return {
            "status": "ERROR",
            "error": str(e)
        }
    except Exception as e:
        if debug:
            print(f"[DEBUG] API error: {e}")
        return {
            "status": "ERROR",
            "error": str(e)
        }

def determine_status_by_timestamp(station):
    """กำหนดสถานะจาก timestamp"""
    if not station.get('date'):
        return 'DISCONNECT'
    
    dt = _parse_date(station['date'])
    if not dt:
        return 'DISCONNECT'
    
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delay = now - dt
    
    if delay <= timedelta(minutes=30):
        return 'ONLINE'
    elif delay <= timedelta(hours=6):
        return 'TIMEOUT'
    else:
        return 'DISCONNECT'

def determine_final_status(station, all_status_dict=None):
    """รวมสถานะจากหลายแหล่ง"""
    if all_status_dict and station.get("station_code"):
        status_info = all_status_dict.get(station["station_code"])
        if status_info and status_info.get("status") != "UNKNOWN":
            return status_info["status"]
    
    if station.get("status_text"):
        status_upper = station["status_text"].upper()
        if any(kw in status_upper for kw in ["ONLINE", "NORMAL", "ACTIVE"]):
            return "ONLINE"
        elif "OFFLINE" in status_upper:
            return "OFFLINE"
        elif "TIMEOUT" in status_upper:
            return "TIMEOUT"
        elif "DISCONNECT" in status_upper:
            return "DISCONNECT"
    
    icon_status = station.get("status_from_icon")
    if icon_status and icon_status != "UNKNOWN":
        return icon_status
    
    return determine_status_by_timestamp(station)

def parse_setmap_from_html(html, all_status_dict=None, debug=False):
    stations = []
    count = 0
    
    for m in re.finditer(r'SetMap\s*\(', html):
        i = m.end()
        depth = 1
        start = i
        while i < len(html) and depth:
            if html[i] == '(':
                depth += 1
            elif html[i] == ')':
                depth -= 1
            i += 1
        
        inner = html[start:i-1].strip()
        if not inner:
            continue
        
        args = _tokenize_args(inner)
        clean = [_clean_str(a) for a in args]
        parsed = [_parse_options(a) if a.startswith('{') and a.endswith('}') else _try_num(a) for a in clean]
        
        if count == 0 and len(parsed) > 0 and str(parsed[0]).lower() == 'lat':
            if debug:
                print("[DEBUG] Skipping SetMap #1 (header labels)")
            count += 1
            continue
        
        if debug and count < 4:
            print(f"\n[DEBUG] SetMap #{count} - Total args: {len(parsed)}")
            for idx, val in enumerate(parsed[:15]):
                val_str = str(val)[:100] if val else "None"
                print(f"  [{idx}] = {val_str}")
        
        st = {
            "lat": parsed[0] if len(parsed) > 0 else None,
            "lon": parsed[1] if len(parsed) > 1 else None,
            "icon_config": parsed[2] if len(parsed) > 2 else None,
            "marker_type": parsed[3] if len(parsed) > 3 else None,
            "image_path": parsed[4] if len(parsed) > 4 else None,
            "name": parsed[5] if len(parsed) > 5 else None,
            "info_html": parsed[6] if len(parsed) > 6 else None,
            "icon_filename": parsed[7] if len(parsed) > 7 else None,
            "code": parsed[8] if len(parsed) > 8 else None,
            "radar_radius": parsed[9] if len(parsed) > 9 else None,
            "label_lat": parsed[10] if len(parsed) > 10 else None,
            "label_lon": parsed[11] if len(parsed) > 11 else None,
            "radar_type": parsed[12] if len(parsed) > 12 else None,
            "radar_name": parsed[13] if len(parsed) > 13 else None,
            "radar_address": parsed[14] if len(parsed) > 14 else None,
        }
        
        st["status_from_icon"] = parse_status_from_icon(st.get("icon_filename"))
        st.update(_parse_info_html(st["info_html"]))
        st["station_code"] = st.get("code")  # ← เพิ่มบรรทัดนี้
        st["status"] = determine_final_status(st, all_status_dict)
        
        stations.append(st)
        count += 1
    
    return stations

def _parse_date(s):
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M UTC", "%d/%m/%Y %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if "UTC" not in str(s) and dt:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except:
            pass
    return None

def clean_data(records):
    out = {}
    for r in records:
        code = (r.get("code") or r.get("station_code") or "").strip()
        if not code:
            continue
        dt = _parse_date(r.get("date"))
        rec = {
            **r,
            "station_code": code,
            "rain_mm": _to_mm(r.get("rain")),
            "date_iso": dt.isoformat() if dt else None
        }
        existing_dt = _parse_date(out[code].get("date")) if code in out else None
        if code not in out or (dt and existing_dt and existing_dt < dt):
            out[code] = rec
    return list(out.values())

def _to_mm(v):
    if not v or not isinstance(v, str):
        return None
    m = re.search(r'(-?\d+(\.\d+)?)', v)
    return float(m.group(1)) if m else None

def save_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

def save_csv(data, path):
    if not data:
        return
    keys = ["station_code", "name", "lat", "lon", "status", "rain", "rain_mm", "date", "date_iso",
            "temperature_c", "humidity_pct", "battery_v", "solar_volt_v", "icon_filename", "image_path"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        for r in data:
            w.writerow({k: r.get(k) for k in keys})

def main(debug=True, test_api=False):
    try:
        print("=" * 60)
        print("🌧️  EEC Rain Gauge Monitor - Enhanced Version")
        print("=" * 60)
        
        sess = login("User", "User@1234", debug=debug)
        
        if debug:
            print("\n[DEBUG] Step 1: Fetching all stations status...")
        all_status_dict = fetch_all_stations_status(sess, debug=debug)
        
        if debug:
            print(f"\n[DEBUG] Step 2: Fetching map page...")
        
        html = sess.get(MAP_URL, timeout=20).text
        
        if debug:
            with open("debug_map.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("[DEBUG] ✓ Saved HTML to debug_map.html")
        
        if debug:
            print("\n[DEBUG] Step 3: Parsing SetMap data...\n")
        
        raw = parse_setmap_from_html(html, all_status_dict, debug=debug)
        
        if test_api and raw:
            print("\n[DEBUG] Step 4: Testing Summary Station API on 3 stations...")
            for st in raw[:3]:
                station_id = st.get("code")
                if station_id:
                    api_result = fetch_station_status_api(sess, station_id, debug=True)
                    if api_result:
                        st["status_from_api"] = api_result.get("status")
                        st["api_response"] = api_result
                        
                        all_latest_status = all_status_dict.get(station_id, {}).get("status", "N/A")
                        print(f"  {station_id}:")
                        print(f"    All_Latest={all_latest_status} | Icon={st.get('status_from_icon')} | API={api_result.get('status')} | Final={st.get('status')}")
                    else:
                        print(f"  {station_id}: API returned None")
                else:
                    print("  No station code found!")
        
        if debug:
            print(f"\n[DEBUG] Step 5: Cleaning and saving data...")
        
        cleaned = clean_data(raw)
        save_json(cleaned, "stations.json")
        save_csv(cleaned, "stations.csv")
        
        print(f"\n✅ Parsed {len(cleaned)} stations")
        print("   → stations.json")
        print("   → stations.csv")
        
        status_count = {}
        for st in cleaned:
            status = st.get("status", "UNKNOWN")
            status_count[status] = status_count.get(status, 0) + 1
        
        print("\n📊 Status Summary:")
        print("-" * 40)
        status_icons = {
            "ONLINE": "🟢",
            "OFFLINE": "🔴",
            "TIMEOUT": "🟡",
            "DISCONNECT": "⚫",
            "REPAIR": "🔧",
            "UNKNOWN": "❓"
        }
        for status in sorted(status_count.keys()):
            count = status_count[status]
            icon = status_icons.get(status, "•")
            pct = (count / len(cleaned) * 100) if cleaned else 0
            print(f"  {icon} {status:12s}: {count:3d} stations ({pct:5.1f}%)")
        
        print("\n📍 Sample Stations (first 10):")
        print("-" * 80)
        for st in cleaned[:10]:
            status_icon = status_icons.get(st.get('status', 'UNKNOWN'), '•')
            station_code = st['station_code']
            
            all_latest_info = all_status_dict.get(station_code, {})
            all_latest_status = all_latest_info.get("status", "N/A")
            
            print(f"\n{status_icon} {station_code}: {st['name']}")
            print(f"   Status: {st.get('status', 'N/A')} (from All_Latest: {all_latest_status})")
            print(f"   Rain: {st.get('rain', 'N/A')} | Temp: {st.get('temperature_c', 'N/A')}°C")
            print(f"   Battery: {st.get('battery_v', 'N/A')}V | Solar: {st.get('solar_volt_v', 'N/A')}V")
            print(f"   Last Update: {st.get('date', 'N/A')}")
            if st.get('icon_filename'):
                print(f"   Icon: {st['icon_filename']}")
            if st.get('station_code'):
                print(f"   🔗 Details: {SUMMARY_URL}?id={st['station_code']}")
        
        print("\n" + "=" * 60)
        print("✨ Complete!")
        print("=" * 60)
            
    except Exception as e:
        print(f"\n❌ [ERROR] {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main(debug=True, test_api=False)