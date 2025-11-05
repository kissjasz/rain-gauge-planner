# maintenance_dashboard.py - ระบบติดตามการบำรุงรักษาสถานีวัดน้ำฝน
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

class MaintenanceDashboard:
    def __init__(self, stations_json_path='stations.json'):
        """โหลดข้อมูลสถานี"""
        with open(stations_json_path, 'r', encoding='utf-8') as f:
            self.stations = json.load(f)
        
        # เกณฑ์การประเมิน
        self.thresholds = {
            'battery': {
                'critical': 10.0,  # < 10V = วิกฤต
                'warning': 11.5,   # < 11.5V = เตือน
                'good': 12.0       # >= 12V = ดี
            },
            'solar': {
                'critical': 5.0,   # < 5V = วิกฤต
                'warning': 10.0,   # < 10V = เตือน
                'good': 13.0       # >= 13V = ดี
            },
            'timeout': {
                'critical': 24,    # > 24 ชม. = วิกฤต
                'warning': 6,      # > 6 ชม. = เตือน
            }
        }
    
    def analyze_battery_health(self):
        """วิเคราะห์สุขภาพแบตเตอรี่ทั้งหมด"""
        battery_status = {
            'critical': [],  # ต้องเปลี่ยนด่วน
            'warning': [],   # ควรติดตาม
            'good': [],      # สภาพดี
            'no_data': []    # ไม่มีข้อมูล
        }
        
        for station in self.stations:
            code = station['station_code']
            name = station['name']
            battery_v = station.get('battery_v')
            solar_v = station.get('solar_volt_v')
            
            if battery_v is None:
                battery_status['no_data'].append({
                    'code': code,
                    'name': name,
                    'reason': 'ไม่มีข้อมูลแบตเตอรี่'
                })
                continue
            
            # ประเมินสถานะ
            if battery_v < self.thresholds['battery']['critical']:
                level = 'critical'
                reason = f'แบตต่ำวิกฤต ({battery_v}V < {self.thresholds["battery"]["critical"]}V)'
            elif battery_v < self.thresholds['battery']['warning']:
                level = 'warning'
                reason = f'แบตต่ำ ({battery_v}V < {self.thresholds["battery"]["warning"]}V)'
            else:
                level = 'good'
                reason = f'สภาพดี ({battery_v}V)'
            
            battery_status[level].append({
                'code': code,
                'name': name,
                'battery_v': battery_v,
                'solar_v': solar_v,
                'reason': reason,
                'last_update': station.get('date')
            })
        
        return battery_status
    
    def find_timeout_stations(self):
        """หาสถานีที่ Timeout พร้อมระยะเวลา"""
        now = datetime.now(timezone.utc)
        timeout_stations = []
        
        for station in self.stations:
            date_str = station.get('date')
            if not date_str:
                continue
            
            # Parse วันที่
            try:
                if 'UTC' in date_str:
                    dt = datetime.strptime(date_str, '%d/%m/%Y %H:%M UTC')
                else:
                    dt = datetime.strptime(date_str, '%d/%m/%Y %H:%M')
                dt = dt.replace(tzinfo=timezone.utc)
            except:
                continue
            
            # คำนวณเวลาที่ล่าช้า
            delay = now - dt
            hours = delay.total_seconds() / 3600
            
            # กรองเฉพาะที่ล่าช้า
            if hours > 1:  # ล่าช้ามากกว่า 1 ชั่วโมง
                level = 'critical' if hours > self.thresholds['timeout']['critical'] else 'warning'
                
                timeout_stations.append({
                    'code': station['station_code'],
                    'name': station['name'],
                    'last_update': date_str,
                    'hours_ago': round(hours, 1),
                    'level': level,
                    'battery_v': station.get('battery_v'),
                    'solar_v': station.get('solar_volt_v'),
                    'status': station.get('status')
                })
        
        # เรียงตามเวลาที่ล่าช้ามากสุด
        timeout_stations.sort(key=lambda x: x['hours_ago'], reverse=True)
        return timeout_stations
    
    def maintenance_priority_list(self):
        """สร้างรายการสถานีที่ต้องบำรุงรักษา เรียงตามความเร่งด่วน"""
        priority_list = []
        
        for station in self.stations:
            code = station['station_code']
            name = station['name']
            score = 0  # คะแนนความเร่งด่วน (สูง = เร่งด่วนมาก)
            issues = []
            
            # 1. เช็คแบตเตอรี่
            battery_v = station.get('battery_v')
            if battery_v:
                if battery_v < self.thresholds['battery']['critical']:
                    score += 100
                    issues.append(f'🔴 แบตวิกฤต {battery_v}V')
                elif battery_v < self.thresholds['battery']['warning']:
                    score += 50
                    issues.append(f'🟡 แบตต่ำ {battery_v}V')
            
            # 2. เช็คโซล่าเซลล์
            solar_v = station.get('solar_volt_v')
            if solar_v:
                if solar_v < self.thresholds['solar']['critical']:
                    score += 80
                    issues.append(f'🔴 โซล่าวิกฤต {solar_v}V')
                elif solar_v < self.thresholds['solar']['warning']:
                    score += 40
                    issues.append(f'🟡 โซล่าต่ำ {solar_v}V')
            
            # 3. เช็คสถานะ
            status = station.get('status', 'UNKNOWN')
            if status == 'DISCONNECT':
                score += 200
                issues.append('🔴 ขาดการติดต่อ')
            elif status == 'TIMEOUT':
                score += 150
                issues.append('🟡 หมดเวลา')
            elif status == 'OFFLINE':
                score += 180
                issues.append('🔴 ออฟไลน์')
            
            # 4. เช็คอุณหภูมิผิดปกติ
            temp = station.get('temperature_c')
            if temp:
                if temp < 10 or temp > 45:
                    score += 30
                    issues.append(f'⚠️ อุณหภูมิผิดปกติ {temp}°C')
            
            # เฉพาะสถานีที่มีปัญหา
            if score > 0:
                priority_list.append({
                    'code': code,
                    'name': name,
                    'priority_score': score,
                    'issues': issues,
                    'battery_v': battery_v,
                    'solar_v': solar_v,
                    'status': status,
                    'last_update': station.get('date'),
                    'lat': station.get('lat'),
                    'lon': station.get('lon')
                })
        
        # เรียงตามความเร่งด่วน
        priority_list.sort(key=lambda x: x['priority_score'], reverse=True)
        return priority_list
    
    def generate_maintenance_report(self):
        """สร้างรายงานสรุปสำหรับทีมบำรุงรักษา"""
        print("=" * 80)
        print("🔧 รายงานการบำรุงรักษาสถานีวัดน้ำฝน")
        print("=" * 80)
        print(f"📅 วันที่: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        print(f"📊 จำนวนสถานีทั้งหมด: {len(self.stations)} สถานี\n")
        
        # 1. สุขภาพแบตเตอรี่
        print("🔋 สุขภาพแบตเตอรี่")
        print("-" * 80)
        battery_health = self.analyze_battery_health()
        print(f"  🔴 วิกฤต (< {self.thresholds['battery']['critical']}V): {len(battery_health['critical'])} สถานี")
        print(f"  🟡 เตือน (< {self.thresholds['battery']['warning']}V): {len(battery_health['warning'])} สถานี")
        print(f"  🟢 ปกติ (>= {self.thresholds['battery']['good']}V): {len(battery_health['good'])} สถานี")
        print(f"  ⚪ ไม่มีข้อมูล: {len(battery_health['no_data'])} สถานี\n")
        
        # แสดงรายละเอียดแบตวิกฤต
        if battery_health['critical']:
            print("  🔴 รายการแบตเตอรี่วิกฤต (ต้องดำเนินการทันที):")
            for st in battery_health['critical'][:5]:
                print(f"     • {st['code']}: {st['name']}")
                print(f"       ├─ แบต: {st['battery_v']}V | โซล่า: {st['solar_v']}V")
                print(f"       └─ {st['reason']}\n")
        
        # 2. สถานี Timeout
        print("\n⏰ สถานีที่ขาดการติดต่อ")
        print("-" * 80)
        timeout_stations = self.find_timeout_stations()
        critical_timeout = [s for s in timeout_stations if s['level'] == 'critical']
        warning_timeout = [s for s in timeout_stations if s['level'] == 'warning']
        
        print(f"  🔴 วิกฤต (> {self.thresholds['timeout']['critical']} ชม.): {len(critical_timeout)} สถานี")
        print(f"  🟡 เตือน (> {self.thresholds['timeout']['warning']} ชม.): {len(warning_timeout)} สถานี\n")
        
        if critical_timeout:
            print("  🔴 รายการ Timeout วิกฤต:")
            for st in critical_timeout[:5]:
                print(f"     • {st['code']}: {st['name']}")
                print(f"       ├─ ล่าสุด: {st['last_update']} ({st['hours_ago']} ชม. ที่แล้ว)")
                print(f"       ├─ แบต: {st['battery_v']}V | โซล่า: {st['solar_v']}V")
                print(f"       └─ สถานะ: {st['status']}\n")
        
        # 3. รายการบำรุงรักษาตามลำดับความสำคัญ
        print("\n📋 รายการบำรุงรักษาตามลำดับความเร่งด่วน")
        print("-" * 80)
        priority_list = self.maintenance_priority_list()
        
        if not priority_list:
            print("  ✅ ไม่มีสถานีที่ต้องบำรุงรักษาด่วน\n")
        else:
            print(f"  พบ {len(priority_list)} สถานีที่ต้องตรวจสอบ\n")
            
            for i, st in enumerate(priority_list[:10], 1):
                print(f"  {i}. [{st['priority_score']} คะแนน] {st['code']}: {st['name']}")
                print(f"     ปัญหา: {', '.join(st['issues'])}")
                print(f"     พิกัด: ({st['lat']}, {st['lon']})")
                print(f"     ล่าสุด: {st['last_update']}\n")
        
        print("=" * 80)
        print("✨ จบรายงาน")
        print("=" * 80)
    
    def export_maintenance_route(self, output_file='maintenance_route.json'):
        """ส่งออกเส้นทางบำรุงรักษาสำหรับใช้กับ Route Planner"""
        priority_list = self.maintenance_priority_list()
        
        # สร้างข้อมูลเส้นทาง
        route_data = {
            'metadata': {
                'created_at': datetime.now(timezone.utc).isoformat(),
                'total_stations': len(priority_list),
                'purpose': 'maintenance'
            },
            'waypoints': []
        }
        
        for st in priority_list:
            route_data['waypoints'].append({
                'station_code': st['code'],
                'name': st['name'],
                'lat': st['lat'],
                'lon': st['lon'],
                'priority_score': st['priority_score'],
                'issues': st['issues'],
                'battery_v': st['battery_v'],
                'solar_v': st['solar_v'],
                'status': st['status'],
                'last_update': st['last_update']
            })
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(route_data, f, ensure_ascii=False, indent=2)
        
        print(f"✅ ส่งออกเส้นทางบำรุงรักษาไปที่ {output_file}")
        return route_data

def main():
    """รันรายงานการบำรุงรักษา"""
    dashboard = MaintenanceDashboard('stations.json')
    
    # สร้างรายงาน
    dashboard.generate_maintenance_report()
    
    # ส่งออกเส้นทาง
    print("\n📍 กำลังสร้างเส้นทางบำรุงรักษา...")
    route_data = dashboard.export_maintenance_route()
    print(f"   จำนวนสถานีที่ต้องบำรุงรักษา: {len(route_data['waypoints'])} สถานี")

if __name__ == "__main__":
    main()