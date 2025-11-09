import streamlit as st
import pandas as pd
import json
import numpy as np
from datetime import datetime, timedelta
import pathlib
import warnings
# Removed sklearn dependencies - using numpy instead
warnings.filterwarnings('ignore')

# Set page configuration prefix for session state
PAGE_KEY_PREFIX = "degradation_"

# Custom CSS for better styling
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        color: #e74c3c;
        text-align: center;
        margin-bottom: 2rem;
    }
    .metric-card {
        background-color: #f0f2f6;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 5px solid #e74c3c;
    }
    .warning-box {
        background-color: #fff3cd;
        border: 1px solid #ffeaa7;
        border-radius: 0.5rem;
        padding: 1rem;
        margin: 1rem 0;
    }
    .success-box {
        background-color: #d4edda;
        border: 1px solid #c3e6cb;
        border-radius: 0.5rem;
        padding: 1rem;
        margin: 1rem 0;
    }
    .critical-box {
        background-color: #f8d7da;
        border: 1px solid #f5c6cb;
        border-radius: 0.5rem;
        padding: 1rem;
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)

@st.cache_data(ttl=600)
def load_latest():
    """โหลดข้อมูลจาก data/latest.json"""
    BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
    DATA_DIR = BASE_DIR / "data"
    LATEST_PATH = DATA_DIR / "latest.json"
    
    try:
        with open(LATEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Extract station data
        stations = data.get("stations", [])
        df = pd.DataFrame(stations)
        
        # Normalize station ID column
        if 'station_id' not in df.columns:
            if 'station_code' in df.columns:
                df['station_id'] = df['station_code']
            elif 'code' in df.columns:
                df['station_id'] = df['code']
        
        # Convert date columns
        if 'date_iso' in df.columns:
            df['timestamp'] = pd.to_datetime(df['date_iso'], errors='coerce')
        elif 'date' in df.columns:
            df['timestamp'] = pd.to_datetime(df['date'], errors='coerce')
        
        # Filter out stations with missing data
        df = df.dropna(subset=['battery_v', 'solar_volt_v', 'timestamp'])
        
        return df
    
    except Exception as e:
        st.error(f"❌ โหลดข้อมูลล้มเหลว: {e}")
        return pd.DataFrame()

def calculate_timeout_speed(df):
    """คำนวณความเร็วในการ timeout (เวลาระหว่างชาร์จเต็มถึง timeout)"""
    if df.empty:
        return pd.DataFrame()
    
    timeout_speeds = []
    
    for station_id in df['station_id'].unique():
        station_data = df[df['station_id'] == station_id].copy().sort_values('timestamp')
        
        if len(station_data) < 5:
            continue
        
        # หาจุดที่แบตเตอรี่เต็ม (>= 14V) และจุดที่ timeout
        full_charge_points = station_data[station_data['battery_v'] >= 14.0]
        timeout_points = station_data[station_data['status'] == 'TIMEOUT']
        
        if len(full_charge_points) == 0 or len(timeout_points) == 0:
            continue
        
        # คำนวณเวลาเฉลี่ยระหว่างการชาร์จเต็มถึง timeout
        for _, full_charge in full_charge_points.iterrows():
            subsequent_timeouts = timeout_points[timeout_points['timestamp'] > full_charge['timestamp']]
            
            if not subsequent_timeouts.empty:
                first_timeout = subsequent_timeouts.iloc[0]
                time_diff = (first_timeout['timestamp'] - full_charge['timestamp']).total_seconds() / (24 * 3600)  # วัน
                
                # คำนวณอัตราการลดของแรงดันโซลาร์
                solar_diff = full_charge['solar_volt_v'] - first_timeout['solar_volt_v']
                timeout_speed = solar_diff / time_diff if time_diff > 0 else 0
                
                timeout_speeds.append({
                    'station_id': station_id,
                    'station_name': station_data.iloc[-1].get('name_th', station_data.iloc[-1].get('name', 'Unknown')),
                    'timeout_speed': timeout_speed,
                    'time_to_timeout_days': time_diff,
                    'full_charge_voltage': full_charge['battery_v'],
                    'timeout_voltage': first_timeout['battery_v'],
                    'full_charge_solar': full_charge['solar_volt_v'],
                    'timeout_solar': first_timeout['solar_volt_v'],
                    'timestamp': first_timeout['timestamp']
                })
    
    return pd.DataFrame(timeout_speeds)

def calculate_mtbf(df):
    """คำนวณ Mean Time Between Failures (MTBF)"""
    if df.empty:
        return pd.DataFrame()
    
    mtbf_data = []
    
    for station_id in df['station_id'].unique():
        station_data = df[df['station_id'] == station_id].copy().sort_values('timestamp')
        
        if len(station_data) < 2:
            continue
        
        # หาช่วงเวลาที่เกิด failure (TIMEOUT หรือ DISCONNECT)
        failure_points = station_data[station_data['status'].isin(['TIMEOUT', 'DISCONNECT'])]
        
        if len(failure_points) < 2:
            continue
        
        # คำนวณระยะเวลาระหว่าง failures
        failure_times = failure_points['timestamp'].values
        time_diffs = np.diff(failure_times) / (24 * 3600)  # แปลงเป็นวัน
        
        if len(time_diffs) > 0:
            mtbf = np.mean(time_diffs)
            mtbf_data.append({
                'station_id': station_id,
                'station_name': station_data.iloc[-1].get('name_th', station_data.iloc[-1].get('name', 'Unknown')),
                'mtbf_days': mtbf,
                'failure_count': len(failure_points),
                'avg_time_between_failures': mtbf,
                'last_failure': failure_points.iloc[-1]['timestamp']
            })
    
    return pd.DataFrame(mtbf_data)

def calculate_outage_durations(df):
    """คำนวณระยะเวลาของการหยุดทำงาน"""
    if df.empty:
        return pd.DataFrame()
    
    outage_data = []
    
    for station_id in df['station_id'].unique():
        station_data = df[df['station_id'] == station_id].copy().sort_values('timestamp')
        
        if len(station_data) < 2:
            continue
        
        # หาช่วงเวลาที่เกิด outage
        outage_starts = station_data[station_data['status'].isin(['TIMEOUT', 'DISCONNECT'])]
        
        for _, outage_start in outage_starts.iterrows():
            # หาจุดที่กลับมาทำงานปกติ
            subsequent_normal = station_data[
                (station_data['timestamp'] > outage_start['timestamp']) & 
                (station_data['status'] == 'ONLINE')
            ]
            
            if not subsequent_normal.empty:
                recovery = subsequent_normal.iloc[0]
                duration = (recovery['timestamp'] - outage_start['timestamp']).total_seconds() / 3600  # ชั่วโมง
                
                outage_data.append({
                    'station_id': station_id,
                    'station_name': station_data.iloc[-1].get('name_th', station_data.iloc[-1].get('name', 'Unknown')),
                    'outage_start': outage_start['timestamp'],
                    'outage_end': recovery['timestamp'],
                    'duration_hours': duration,
                    'outage_type': outage_start['status']
                })
    
    return pd.DataFrame(outage_data)

def predict_failure_probability(df, days_ahead=7):
    """ทำนายความน่าจะเป็นในการเกิด timeout 7 วันข้างหน้า"""
    if df.empty:
        return pd.DataFrame()
    
    predictions = []
    
    for station_id in df['station_id'].unique():
        station_data = df[df['station_id'] == station_id].copy().sort_values('timestamp')
        
        if len(station_data) < 10:
            continue
        
        # สร้าง features สำหรับการทำนาย
        station_data['days_since_start'] = (station_data['timestamp'] - station_data['timestamp'].min()).dt.days
        station_data['battery_trend'] = station_data['battery_v'].rolling(window=5).apply(lambda x: np.polyfit(range(len(x)), x, 1)[0] if len(x) > 1 else 0)
        station_data['solar_trend'] = station_data['solar_volt_v'].rolling(window=5).apply(lambda x: np.polyfit(range(len(x)), x, 1)[0] if len(x) > 1 else 0)
        
        # สร้าง target variable (1 if timeout, 0 otherwise)
        station_data['is_timeout'] = (station_data['status'] == 'TIMEOUT').astype(int)
        
        # ใช้ linear regression สำหรับการทำนางอย่างง่าย
        features = ['days_since_start', 'battery_v', 'solar_volt_v', 'battery_trend', 'solar_trend']
        X = station_data[features].fillna(0)
        y = station_data['is_timeout']
        
        if len(X) > 5 and y.sum() > 0:  # ต้องมีข้อมูล timeout บ้าง
            try:
                # Use numpy for linear regression instead of sklearn
                X_arr = np.array(X)
                y_arr = np.array(y)
                
                # Fit linear regression for each feature
                coefficients = []
                for i in range(X_arr.shape[1]):
                    # Simple linear regression for each feature against y
                    a, b = np.polyfit(X_arr[:, i], y_arr, 1)
                    coefficients.append((a, b))
                
                # ทำนาย 7 วันข้างหน้า
                last_data = station_data.iloc[-1].copy()
                future_predictions = []
                
                for day in range(1, days_ahead + 1):
                    future_data = {
                        'days_since_start': last_data['days_since_start'] + day,
                        'battery_v': last_data['battery_v'] + (last_data.get('battery_trend', 0) * day),
                        'solar_volt_v': last_data['solar_volt_v'] + (last_data.get('solar_trend', 0) * day),
                        'battery_trend': last_data.get('battery_trend', 0),
                        'solar_trend': last_data.get('solar_trend', 0)
                    }
                    
                    # Calculate prediction using numpy regression
                    prob = 0
                    for i, (a, b) in enumerate(coefficients):
                        feature_value = list(future_data.values())[i]
                        prob += a * feature_value + b
                    
                    # Average the predictions from all features
                    prob /= len(coefficients)
                    future_predictions.append(max(0, min(1, prob)))  #  clamp between 0 and 1
                
                avg_probability = np.mean(future_predictions)
                
                predictions.append({
                    'station_id': station_id,
                    'station_name': station_data.iloc[-1].get('name_th', station_data.iloc[-1].get('name', 'Unknown')),
                    'failure_probability_7d': avg_probability,
                    'current_battery': last_data['battery_v'],
                    'current_solar': last_data['solar_volt_v'],
                    'battery_trend': last_data.get('battery_trend', 0),
                    'solar_trend': last_data.get('solar_trend', 0),
                    'last_update': last_data['timestamp']
                })
            except:
                continue
    
    return pd.DataFrame(predictions)

def calculate_composite_degradation_score(df, timeout_speeds, mtbf_data, failure_probs):
    """คำนวณคะแนนการเสื่อมสภาพแบบ composite"""
    if df.empty:
        return pd.DataFrame()
    
    degradation_scores = []
    
    for station_id in df['station_id'].unique():
        station_data = df[df['station_id'] == station_id].copy().sort_values('timestamp')
        
        if len(station_data) < 5:
            continue
        
        # ดึงข้อมูลล่าสุด
        latest_data = station_data.iloc[-1]
        
        # คำนวณ ΔV/day (battery voltage decay rate)
        if len(station_data) >= 2:
            time_diff = (station_data.iloc[-1]['timestamp'] - station_data.iloc[0]['timestamp']).total_seconds() / (24 * 3600)
            voltage_diff = station_data.iloc[0]['battery_v'] - station_data.iloc[-1]['battery_v']
            decay_rate = voltage_diff / time_diff if time_diff > 0 else 0
        else:
            decay_rate = 0
        
        # ดึงข้อมูล timeout speed
        timeout_speed = 0
        if not timeout_speeds.empty:
            station_timeout = timeout_speeds[timeout_speeds['station_id'] == station_id]
            if not station_timeout.empty:
                timeout_speed = station_timeout.iloc[0]['timeout_speed']
        
        # ดึงข้อมูล MTBF
        mtbf = 999  # ค่าเริ่มต้นสูงๆ
        if not mtbf_data.empty:
            station_mtbf = mtbf_data[mtbf_data['station_id'] == station_id]
            if not station_mtbf.empty:
                mtbf = station_mtbf.iloc[0]['mtbf_days']
        
        # ดึงข้อมูลความน่าจะเป็นในการเกิด failure
        failure_prob = 0
        if not failure_probs.empty:
            station_prob = failure_probs[failure_probs['station_id'] == station_id]
            if not station_prob.empty:
                failure_prob = station_prob.iloc[0]['failure_probability_7d']
        
        # คำนวณคะแนน composite (normalized)
        # ยิ่งค่าสูง แสดงว่าเสื่อมมากขึ้น
        decay_score = min(decay_rate * 10, 5)  # จำกัดไว้ที่ 5
        timeout_score = min(abs(timeout_speed) * 2, 5)  # จำกัดไว้ที่ 5
        solar_score = max(0, (15 - latest_data['solar_volt_v']) / 3)  # ยิ่งโซลาร์ต่ำ คะแนนสูง
        mtbf_score = max(0, (30 - mtbf) / 6)  # ยิ่ง MTBF ต่ำ คะแนนสูง
        prob_score = failure_prob * 5  # ความน่าจะเป็นในการเกิด failure
        
        composite_score = decay_score + timeout_score + solar_score + mtbf_score + prob_score
        
        degradation_scores.append({
            'station_id': station_id,
            'station_name': latest_data.get('name_th', latest_data.get('name', 'Unknown')),
            'composite_score': composite_score,
            'decay_rate': decay_rate,
            'timeout_speed': timeout_speed,
            'solar_voltage': latest_data['solar_volt_v'],
            'mtbf': mtbf,
            'failure_probability': failure_prob,
            'current_battery': latest_data['battery_v'],
            'last_update': latest_data['timestamp']
        })
    
    return pd.DataFrame(degradation_scores)

def create_timeout_speed_chart(timeout_speeds):
    """สร้างกราฟ Timeout Speed Monitor"""
    if timeout_speeds.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    fig = go.Figure()
    
    # เรียงลำดับตาม timeout speed
    sorted_data = timeout_speeds.sort_values('timeout_speed', ascending=True)
    
    fig.add_trace(go.Scatter(
        x=sorted_data['timeout_speed'],
        y=sorted_data['station_id'],
        mode='markers',
        name='Timeout Speed',
        marker=dict(
            color=sorted_data['timeout_speed'],
            colorscale='Reds',
            size=10,
            colorbar=dict(title="Timeout Speed (V/day)")
        ),
        text=sorted_data.apply(lambda x: f"Station: {x['station_id']}<br>Speed: {x['timeout_speed']:.3f} V/day<br>Time to timeout: {x['time_to_timeout_days']:.1f} days", axis=1),
        hovertemplate='%{text}<extra></extra>'
    ))
    
    fig.update_layout(
        title='Timeout Speed Monitor (เวลาระหว่างชาร์จเต็มถึง timeout)',
        xaxis_title='Timeout Speed (V/day)',
        yaxis_title='Station ID',
        template='plotly_white',
        height=500
    )
    
    return fig

def create_mtbf_trend_chart(mtbf_data):
    """สร้างกราฟ MTBF Trend"""
    if mtbf_data.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    fig = go.Figure()
    
    # เรียงลำดับตาม MTBF
    sorted_data = mtbf_data.sort_values('mtbf_days', ascending=True)
    
    fig.add_trace(go.Scatter(
        x=sorted_data['mtbf_days'],
        y=sorted_data['station_id'],
        mode='markers',
        name='MTBF',
        marker=dict(
            color=sorted_data['mtbf_days'],
            colorscale='Blues',
            size=10,
            colorbar=dict(title="MTBF (days)")
        ),
        text=sorted_data.apply(lambda x: f"Station: {x['station_id']}<br>MTBF: {x['mtbf_days']:.1f} days<br>Failures: {x['failure_count']}", axis=1),
        hovertemplate='%{text}<extra></extra>'
    ))
    
    # เพิ่มเส้นค่าเฉลี่ย
    avg_mtbf = sorted_data['mtbf_days'].mean()
    fig.add_vline(x=avg_mtbf, line_dash="dash", line_color="red", 
                  annotation_text=f"Average MTBF: {avg_mtbf:.1f} days")
    
    fig.update_layout(
        title='MTBF Trend (Mean Time Between Failures)',
        xaxis_title='MTBF (days)',
        yaxis_title='Station ID',
        template='plotly_white',
        height=500
    )
    
    return fig

def create_outage_duration_histogram(outage_data):
    """สร้างกราฟ Outage Duration Distribution"""
    if outage_data.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    fig = go.Figure()
    
    # สร้าง histogram
    fig.add_trace(go.Histogram(
        x=outage_data['duration_hours'],
        nbinsx=20,
        name='Outage Duration',
        marker=dict(color='orange', opacity=0.7)
    ))
    
    # เพิ่มสถิติ
    mean_duration = outage_data['duration_hours'].mean()
    median_duration = outage_data['duration_hours'].median()
    
    fig.add_vline(x=mean_duration, line_dash="dash", line_color="red", 
                  annotation_text=f"Mean: {mean_duration:.1f} hours")
    fig.add_vline(x=median_duration, line_dash="dash", line_color="blue", 
                  annotation_text=f"Median: {median_duration:.1f} hours")
    
    fig.update_layout(
        title='Outage Duration Distribution',
        xaxis_title='Duration (hours)',
        yaxis_title='Frequency',
        template='plotly_white',
        height=400
    )
    
    return fig

def create_failure_probability_forecast(failure_probs):
    """สร้างกราฟ Failure Probability Forecast"""
    if failure_probs.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    fig = go.Figure()
    
    # เรียงลำดับตามความน่าจะเป็น
    sorted_data = failure_probs.sort_values('failure_probability_7d', ascending=False)
    
    fig.add_trace(go.Scatter(
        x=sorted_data['failure_probability_7d'],
        y=sorted_data['station_id'],
        mode='markers',
        name='Failure Probability',
        marker=dict(
            color=sorted_data['failure_probability_7d'],
            colorscale='Reds',
            size=10,
            colorbar=dict(title="Failure Probability (7 days)")
        ),
        text=sorted_data.apply(lambda x: f"Station: {x['station_id']}<br>Probability: {x['failure_probability_7d']:.2%}<br>Battery: {x['current_battery']:.1f}V<br>Solar: {x['current_solar']:.1f}V", axis=1),
        hovertemplate='%{text}<extra></extra>'
    ))
    
    # เพิ่มเส้นค่าเฉลี่ย
    avg_prob = sorted_data['failure_probability_7d'].mean()
    fig.add_vline(x=avg_prob, line_dash="dash", line_color="red", 
                  annotation_text=f"Average Risk: {avg_prob:.2%}")
    
    fig.update_layout(
        title='Failure Probability Forecast (7 วันข้างหน้า)',
        xaxis_title='Failure Probability',
        yaxis_title='Station ID',
        template='plotly_white',
        height=500
    )
    
    return fig

def main():
    """ฟังก์ชันหลักของ Dashboard"""
    st.title("⚠️ Degradation & Risk Dashboard")
    st.caption("การวิเคราะห์ความเสื่อมและความเสี่ยงของสถานีวัดระดับน้ำฝน")
    
    # Load data
    with st.spinner("กำลังโหลดข้อมูลสถานี..."):
        df = load_latest()
    
    if df.empty:
        st.error("❌ ไม่สามารถโหลดข้อมูลสถานีได้")
        st.stop()
    
    # Sidebar filters
    st.sidebar.header("🔧 ตัวกรองข้อมูล")
    
    # Date range filter
    if 'timestamp' in df.columns:
        min_date = df['timestamp'].min().date()
        max_date = df['timestamp'].max().date()
        
        selected_date_range = st.sidebar.date_input(
            "เลือกช่วงวันที่",
            value=[min_date, max_date],
            min_value=min_date,
            max_value=max_date
        )
        
        if len(selected_date_range) == 2:
            start_date, end_date = selected_date_range
            df = df[(df['timestamp'].dt.date >= start_date) & (df['timestamp'].dt.date <= end_date)]
    
    # Calculate all metrics
    with st.spinner("กำลังคำนวณข้อมูลการเสื่อมและความเสี่ยง..."):
        timeout_speeds = calculate_timeout_speed(df)
        mtbf_data = calculate_mtbf(df)
        outage_data = calculate_outage_durations(df)
        failure_probs = predict_failure_probability(df)
        degradation_scores = calculate_composite_degradation_score(df, timeout_speeds, mtbf_data, failure_probs)
    
    # Display key metrics
    st.subheader("📊 ภาพรวมความเสี่ยง")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        avg_mtbf = mtbf_data['mtbf_days'].mean() if not mtbf_data.empty else 0
        st.metric("MTBF เฉลี่ย", f"{avg_mtbf:.1f} วัน")
    
    with col2:
        fastest_timeout = timeout_speeds['timeout_speed'].max() if not timeout_speeds.empty else 0
        st.metric("Timeout เร็วสุด", f"{fastest_timeout:.3f} V/วัน")
    
    with col3:
        avg_decay = degradation_scores['decay_rate'].mean() if not degradation_scores.empty else 0
        st.metric("การเสื่อมเฉลี่ย", f"{avg_decay:.3f} V/วัน")
    
    with col4:
        high_risk_count = len(failure_probs[failure_probs['failure_probability_7d'] > 0.5]) if not failure_probs.empty else 0
        st.metric("สถานีเสี่ยงสูง", high_risk_count)
    
    # Main charts
    st.subheader("📈 กราฟวิเคราะห์ความเสื่อมและความเสี่ยง")
    
    # Timeout Speed Monitor
    st.write("### 1. Timeout Speed Monitor (เวลาระหว่างชาร์จเต็มถึง timeout)")
    timeout_fig = create_timeout_speed_chart(timeout_speeds)
    if timeout_fig:
        st.plotly_chart(timeout_fig, width="stretch")
    
    # MTBF Trend
    st.write("### 2. MTBF Trend (Mean Time Between Failures)")
    mtbf_fig = create_mtbf_trend_chart(mtbf_data)
    if mtbf_fig:
        st.plotly_chart(mtbf_fig, width="stretch")
    
    # Outage Duration Distribution
    st.write("### 3. Outage Duration Distribution")
    outage_fig = create_outage_duration_histogram(outage_data)
    if outage_fig:
        st.plotly_chart(outage_fig, width="stretch")
    
    # Failure Probability Forecast
    st.write("### 4. Failure Probability Forecast (คาดการณ์ timeout 7 วันข้างหน้า)")
    forecast_fig = create_failure_probability_forecast(failure_probs)
    if forecast_fig:
        st.plotly_chart(forecast_fig, width="stretch")
    
    # Top 10 Degraded Stations
    st.subheader("🏆 Top 10 สถานีเสื่อมสภาพมากที่สุด")
    
    if not degradation_scores.empty:
        top_10_degraded = degradation_scores.nlargest(10, 'composite_score')
        
        # Format table
        display_df = top_10_degraded.copy()
        display_df['composite_score'] = display_df['composite_score'].round(2)
        display_df['decay_rate'] = display_df['decay_rate'].round(3)
        display_df['timeout_speed'] = display_df['timeout_speed'].round(3)
        display_df['solar_voltage'] = display_df['solar_voltage'].round(1)
        display_df['mtbf'] = display_df['mtbf'].round(1)
        display_df['failure_probability'] = (display_df['failure_probability'] * 100).round(1)
        display_df['current_battery'] = display_df['current_battery'].round(1)
        display_df['last_update'] = display_df['last_update'].dt.strftime('%Y-%m-%d %H:%M')
        
        display_df = display_df.rename(columns={
            'station_id': 'รหัสสถานี',
            'station_name': 'ชื่อสถานี',
            'composite_score': 'คะแนนรวม',
            'decay_rate': 'ΔV/day',
            'timeout_speed': 'Timeout Speed',
            'solar_voltage': 'โซลาร์ (V)',
            'mtbf': 'MTBF (วัน)',
            'failure_probability': 'ความเสี่ยง (%)',
            'current_battery': 'แบตเตอรี่ (V)',
            'last_update': 'อัปเดตล่าสุด'
        })
        
        st.dataframe(display_df, width="stretch", hide_index=True)
        
        # Add warning for critical stations
        critical_stations = display_df[display_df['คะแนนรวม'] > 10]
        if not critical_stations.empty:
            st.markdown('<div class="critical-box">⚠️ <strong>คำเตือน:</strong> มีสถานีที่มีคะแนนการเสื่อมสูง (>10) ควรตรวจสอบโดยเร่งด่วน!</div>', unsafe_allow_html=True)
    else:
        st.info("ไม่มีข้อมูลการเสื่อมสภาพของสถานี")
    
    # Footer
    st.markdown("---")
    st.markdown('<p style="text-align: center; color: #666;">⚠️ Degradation & Risk Dashboard - Real-time Monitoring System</p>', unsafe_allow_html=True)

if __name__ == "__main__":
    main()