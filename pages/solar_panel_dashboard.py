import streamlit as st
import pandas as pd
import json
import numpy as np
from datetime import datetime, timedelta
import pathlib
import warnings
warnings.filterwarnings('ignore')

# Set page configuration prefix for session state
PAGE_KEY_PREFIX = "solar_"

# Custom CSS for better styling
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        color: #f39c12;
        text-align: center;
        margin-bottom: 2rem;
    }
    .metric-card {
        background-color: #f0f2f6;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 5px solid #f39c12;
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
        
        # Filter out stations with missing solar data
        df = df.dropna(subset=['solar_volt_v', 'timestamp'])
        
        return df
    
    except Exception as e:
        st.error(f"❌ โหลดข้อมูลล้มเหลว: {e}")
        return pd.DataFrame()

def calculate_solar_metrics(df):
    """คำนวณ metrics สำหรับ solar panel performance"""
    if df.empty:
        return {}
    
    # Get the latest reading for each station to avoid counting duplicates
    df_latest = df.sort_values('timestamp').groupby('station_id').tail(1)
    
    # Basic statistics
    metrics = {
        'total_stations': df_latest['station_id'].nunique(),
        'avg_solar_voltage': df_latest['solar_volt_v'].mean(),
        'min_solar_voltage': df_latest['solar_volt_v'].min(),
        'max_solar_voltage': df_latest['solar_volt_v'].max(),
        'std_solar_voltage': df_latest['solar_volt_v'].std(),
        'median_solar_voltage': df_latest['solar_volt_v'].median(),
        'p25_solar_voltage': df_latest['solar_volt_v'].quantile(0.25),
        'p75_solar_voltage': df_latest['solar_volt_v'].quantile(0.75)
    }
    
    # Count stations by voltage ranges (using unique stations)
    metrics['critical_low'] = df_latest[df_latest['solar_volt_v'] < 13.0]['station_id'].nunique()
    metrics['low'] = df_latest[(df_latest['solar_volt_v'] >= 13.0) & (df_latest['solar_volt_v'] < 15.0)]['station_id'].nunique()
    metrics['normal'] = df_latest[(df_latest['solar_volt_v'] >= 15.0) & (df_latest['solar_volt_v'] <= 18.0)]['station_id'].nunique()
    metrics['high'] = df_latest[df_latest['solar_volt_v'] > 18.0]['station_id'].nunique()
    
    # Count by status (using unique stations)
    if 'status' in df_latest.columns:
        metrics['timeout_count'] = df_latest[df_latest['status'] == 'TIMEOUT']['station_id'].nunique()
        metrics['disconnect_count'] = df_latest[df_latest['status'] == 'DISCONNECT']['station_id'].nunique()
        metrics['online_count'] = df_latest[df_latest['status'] == 'ONLINE']['station_id'].nunique()
    
    return metrics

def detect_low_solar_stations(df, threshold_days=3, voltage_threshold=13.0):
    """ตรวจจับสถานีที่มีแรงดันโซลาร์ต่ำเป็นเวลาติดต่อง"""
    if df.empty:
        return pd.DataFrame()
    
    low_solar_stations = []
    
    for station_id in df['station_id'].unique():
        station_data = df[df['station_id'] == station_id].copy()
        
        if len(station_data) < threshold_days:
            continue
        
        # Sort by timestamp
        station_data = station_data.sort_values('timestamp')
        
        # Check for consecutive days with low solar voltage
        station_data['date'] = station_data['timestamp'].dt.date
        station_data['is_low'] = station_data['solar_volt_v'] < voltage_threshold
        
        # Group by date and check if any day has low solar
        daily_low = station_data.groupby('date')['is_low'].any().reset_index()
        
        # Find consecutive days with low solar
        consecutive_count = 0
        max_consecutive = 0
        
        for is_low in daily_low['is_low']:
            if is_low:
                consecutive_count += 1
                max_consecutive = max(max_consecutive, consecutive_count)
            else:
                consecutive_count = 0
        
        if max_consecutive >= threshold_days:
            latest_data = station_data.iloc[-1]
            low_solar_stations.append({
                'station_id': station_id,
                'station_name': latest_data.get('name_th', latest_data.get('name', 'Unknown')),
                'consecutive_low_days': max_consecutive,
                'latest_solar_voltage': latest_data['solar_volt_v'],
                'latest_battery_voltage': latest_data.get('battery_v', np.nan),
                'latest_status': latest_data.get('status', 'UNKNOWN'),
                'last_update': latest_data['timestamp']
            })
    
    return pd.DataFrame(low_solar_stations)

def create_solar_performance_scatter(df):
    """สร้างกราฟ Solar Performance vs Issues"""
    if df.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    # Filter stations with issues (TIMEOUT or DISCONNECT)
    df_issues = df[df['status'].isin(['TIMEOUT', 'DISCONNECT'])].copy()
    df_normal = df[df['status'] == 'ONLINE'].copy()
    
    fig = go.Figure()
    
    # Add normal stations
    if not df_normal.empty:
        fig.add_trace(go.Scatter(
            x=df_normal['solar_volt_v'],
            y=[1] * len(df_normal),
            mode='markers',
            name='ONLINE',
            marker=dict(
                color='green',
                size=8,
                opacity=0.7
            ),
            text=df_normal.apply(lambda x: f"Station: {x['station_id']}<br>Solar: {x['solar_volt_v']:.1f}V", axis=1),
            hovertemplate='%{text}<extra></extra>'
        ))
    
    # Add TIMEOUT stations
    timeout_data = df_issues[df_issues['status'] == 'TIMEOUT']
    if not timeout_data.empty:
        fig.add_trace(go.Scatter(
            x=timeout_data['solar_volt_v'],
            y=[2] * len(timeout_data),
            mode='markers',
            name='TIMEOUT',
            marker=dict(
                color='orange',
                size=10,
                opacity=0.8,
                symbol='triangle-up'
            ),
            text=timeout_data.apply(lambda x: f"Station: {x['station_id']}<br>Solar: {x['solar_volt_v']:.1f}V", axis=1),
            hovertemplate='%{text}<extra></extra>'
        ))
    
    # Add DISCONNECT stations
    disconnect_data = df_issues[df_issues['status'] == 'DISCONNECT']
    if not disconnect_data.empty:
        fig.add_trace(go.Scatter(
            x=disconnect_data['solar_volt_v'],
            y=[3] * len(disconnect_data),
            mode='markers',
            name='DISCONNECT',
            marker=dict(
                color='red',
                size=10,
                opacity=0.8,
                symbol='x'
            ),
            text=disconnect_data.apply(lambda x: f"Station: {x['station_id']}<br>Solar: {x['solar_volt_v']:.1f}V", axis=1),
            hovertemplate='%{text}<extra></extra>'
        ))
    
    # Add threshold lines
    fig.add_vline(x=13.0, line_dash="dash", line_color="red", annotation_text="Min Threshold (13V)")
    fig.add_vline(x=18.0, line_dash="dash", line_color="orange", annotation_text="Max Threshold (18V)")
    
    fig.update_layout(
        title='Solar Performance vs Station Status',
        xaxis_title='Solar Panel Voltage (V)',
        yaxis_title='Station Status',
        yaxis=dict(
            tickvals=[1, 2, 3],
            ticktext=['ONLINE', 'TIMEOUT', 'DISCONNECT']
        ),
        hovermode='closest',
        template='plotly_white',
        height=400
    )
    
    return fig

def create_solar_battery_correlation(df):
    """สร้างกราฟ Solar-Battery Correlation"""
    if df.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    # Filter out rows with missing battery data
    df_corr = df.dropna(subset=['battery_v', 'solar_volt_v']).copy()
    
    if df_corr.empty:
        return None
    
    fig = go.Figure()
    
    # Add scatter plot
    fig.add_trace(go.Scatter(
        x=df_corr['solar_volt_v'],
        y=df_corr['battery_v'],
        mode='markers',
        name='Stations',
        marker=dict(
            color=df_corr['solar_volt_v'],
            colorscale='Viridis',
            size=8,
            opacity=0.7,
            colorbar=dict(title="Solar Voltage (V)")
        ),
        text=df_corr.apply(lambda x: f"Station: {x['station_id']}<br>Solar: {x['solar_volt_v']:.1f}V<br>Battery: {x['battery_v']:.1f}V", axis=1),
        hovertemplate='%{text}<extra></extra>'
    ))
    
    # Add threshold lines
    fig.add_vline(x=13.0, line_dash="dash", line_color="red", annotation_text="Min Solar (13V)")
    fig.add_vline(x=18.0, line_dash="dash", line_color="orange", annotation_text="Max Solar (18V)")
    fig.add_hline(y=12.0, line_dash="dash", line_color="red", annotation_text="Min Battery (12V)")
    
    # Calculate and add trend line
    if len(df_corr) > 1:
        z = np.polyfit(df_corr['solar_volt_v'], df_corr['battery_v'], 1)
        p = np.poly1d(z)
        x_trend = np.linspace(df_corr['solar_volt_v'].min(), df_corr['solar_volt_v'].max(), 100)
        y_trend = p(x_trend)
        
        fig.add_trace(go.Scatter(
            x=x_trend,
            y=y_trend,
            mode='lines',
            name='Trend Line',
            line=dict(color='red', width=2, dash='dot')
        ))
    
    fig.update_layout(
        title='Solar Panel Voltage vs Battery Voltage Correlation',
        xaxis_title='Solar Panel Voltage (V)',
        yaxis_title='Battery Voltage (V)',
        hovermode='closest',
        template='plotly_white',
        height=400
    )
    
    return fig

def create_solar_daily_profile(df):
    """สร้างกราฟ Solar Voltage Daily Profile"""
    if df.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    # Create hourly profile
    df['hour'] = df['timestamp'].dt.hour
    hourly_stats = df.groupby('hour')['solar_volt_v'].agg(['mean', 'median', 'std']).reset_index()
    
    fig = go.Figure()
    
    # Add median line
    fig.add_trace(go.Scatter(
        x=hourly_stats['hour'],
        y=hourly_stats['median'],
        mode='lines+markers',
        name='Median Solar Voltage',
        line=dict(color='orange', width=3),
        marker=dict(size=6)
    ))
    
    # Add mean line
    fig.add_trace(go.Scatter(
        x=hourly_stats['hour'],
        y=hourly_stats['mean'],
        mode='lines',
        name='Mean Solar Voltage',
        line=dict(color='blue', width=2, dash='dash')
    ))
    
    # Add confidence interval (±1 std)
    fig.add_trace(go.Scatter(
        x=hourly_stats['hour'],
        y=hourly_stats['mean'] + hourly_stats['std'],
        mode='lines',
        line=dict(width=0),
        showlegend=False,
        hoverinfo='skip'
    ))
    
    fig.add_trace(go.Scatter(
        x=hourly_stats['hour'],
        y=hourly_stats['mean'] - hourly_stats['std'],
        mode='lines',
        line=dict(width=0),
        fill='tonexty',
        fillcolor='rgba(255,165,0,0.2)',
        name='±1 Std Dev',
        hoverinfo='skip'
    ))
    
    # Add threshold lines
    fig.add_hline(y=13.0, line_dash="dash", line_color="red", annotation_text="Min Threshold (13V)")
    fig.add_hline(y=18.0, line_dash="dash", line_color="orange", annotation_text="Max Threshold (18V)")
    
    fig.update_layout(
        title='Solar Voltage Daily Profile (Hourly Average)',
        xaxis_title='Hour of Day',
        yaxis_title='Solar Panel Voltage (V)',
        hovermode='x unified',
        template='plotly_white',
        height=400
    )
    
    return fig

def create_undercharge_event_rate(df):
    """สร้างกราฟ Under-charge Event Rate"""
    if df.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    # Create daily analysis
    df['date'] = df['timestamp'].dt.date
    
    # Count under-charge events per day (solar < 13V AND status is TIMEOUT/DISCONNECT)
    daily_events = df[df['solar_volt_v'] < 13.0].copy()
    daily_events = daily_events[daily_events['status'].isin(['TIMEOUT', 'DISCONNECT'])]
    
    if daily_events.empty:
        return None
    
    event_counts = daily_events.groupby('date').size().reset_index(name='undercharge_events')
    
    # Calculate total stations per day for percentage
    daily_totals = df.groupby('date').size().reset_index(name='total_stations')
    
    # Merge and calculate percentage
    event_stats = pd.merge(event_counts, daily_totals, on='date', how='right')
    event_stats['undercharge_events'] = event_stats['undercharge_events'].fillna(0)
    event_stats['event_rate'] = (event_stats['undercharge_events'] / event_stats['total_stations']) * 100
    
    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=('Number of Under-charge Events per Day', 'Under-charge Event Rate (%)'),
        vertical_spacing=0.1
    )
    
    # Add bar chart for event counts
    fig.add_trace(
        go.Bar(
            x=event_stats['date'],
            y=event_stats['undercharge_events'],
            name='Event Count',
            marker_color='red'
        ),
        row=1, col=1
    )
    
    # Add line chart for event rate
    fig.add_trace(
        go.Scatter(
            x=event_stats['date'],
            y=event_stats['event_rate'],
            mode='lines+markers',
            name='Event Rate (%)',
            line=dict(color='orange', width=2),
            marker=dict(size=6)
        ),
        row=2, col=1
    )
    
    fig.update_layout(
        title='Under-charge Event Analysis (Low Solar + High Timeout/Disconnect)',
        height=600,
        template='plotly_white',
        showlegend=True
    )
    
    fig.update_xaxes(title_text="Date", row=2, col=1)
    fig.update_yaxes(title_text="Number of Events", row=1, col=1)
    fig.update_yaxes(title_text="Event Rate (%)", row=2, col=1)
    
    return fig

def main():
    """ฟังก์ชันหลักของ Dashboard"""
    st.title("☀️ Solar Panel Dashboard")
    st.caption("การทำงานของแผงโซลาร์สถานีวัดระดับน้ำฝน")
    
    # Load data
    with st.spinner("กำลังโหลดข้อมูลสถานี..."):
        df = load_latest()
    
    if df.empty:
        st.error("❌ ไม่สามารถโหลดข้อมูลสถานีได้")
        st.stop()
    
    # Create a base dataframe for calculating ONLINE status (not affected by filters)
    df_base = df.copy()
    
    # Calculate ONLINE percentage from base data
    if 'status' in df_base.columns:
        total_stations = df_base['station_id'].nunique()
        online_stations = df_base.loc[df_base['status'].str.upper() == 'ONLINE', 'station_id'].nunique()
        online_pct = round(100 * online_stations / max(total_stations, 1), 1)
    else:
        total_stations = df_base['station_id'].nunique()
        online_pct = 0.0
    
    # Calculate normal voltage percentage from base data
    if 'solar_volt_v' in df_base.columns:
        # Group by station to get the latest reading per station
        df_voltage = df_base.dropna(subset=['solar_volt_v']).copy()
        if not df_voltage.empty:
            # Get the latest reading for each station
            df_voltage = df_voltage.sort_values('timestamp').groupby('station_id').tail(1)
            normal_voltage_count = df_voltage[(df_voltage['solar_volt_v'] >= 15.0) & (df_voltage['solar_volt_v'] <= 18.0)]['station_id'].nunique()
            valid_voltage_count = df_voltage['station_id'].nunique()
            normal_voltage_pct = round(100 * normal_voltage_count / max(valid_voltage_count, 1), 1)
        else:
            normal_voltage_pct = 0.0
    else:
        normal_voltage_pct = 0.0
    
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
    
    # Solar voltage range filter
    voltage_range = st.sidebar.slider(
        "ช่วงแรงดันโซลาร์ (V)",
        min_value=float(df['solar_volt_v'].min()),
        max_value=float(df['solar_volt_v'].max()),
        value=(float(df['solar_volt_v'].min()), float(df['solar_volt_v'].max()))
    )
    
    df = df[(df['solar_volt_v'] >= voltage_range[0]) & (df['solar_volt_v'] <= voltage_range[1])]
    
    # Status filter
    if 'status' in df.columns:
        status_options = df['status'].unique().tolist()
        selected_status = st.sidebar.multiselect(
            "เลือกสถานะสถานี",
            options=status_options,
            default=status_options
        )
        df = df[df['status'].isin(selected_status)]
    
    # Calculate metrics
    metrics = calculate_solar_metrics(df)
    low_solar_stations = detect_low_solar_stations(df)
    
    # Display key metrics
    st.subheader("📊 ภาพรวมสุขภาพแผงโซลาร์")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("จำนวนสถานี", total_stations)
    
    with col2:
        avg_v = metrics.get('avg_solar_voltage', 0)
        st.metric("แรงดันเฉลี่ย", f"{avg_v:.2f} V")
    
    with col3:
        critical = metrics.get('critical_low', 0)
        st.metric("แรงดันต่ำวิกฤต", critical, delta=f"{critical} สถานี")
    
    with col4:
        st.metric("สถานีปกติ (ONLINE)", f"{online_pct:.1f}%")
    
    # Add a second row for the normal voltage metric
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("แรงดันปกติ (15-18V)", f"{normal_voltage_pct:.1f}%")
    
    # Solar status distribution
    col1, col2 = st.columns(2)
    
    with col1:
        # Create pie chart for voltage status
        status_data = {
            'วิกฤตต่ำ (<13V)': metrics.get('critical_low', 0),
            'ต่ำ (13-15V)': metrics.get('low', 0),
            'ปกติ (15-18V)': metrics.get('normal', 0),
            'สูง (>18V)': metrics.get('high', 0)
        }
        
        # Lazy import plotly with error handling
        try:
            import plotly.express as px
        except Exception as e:
            st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
            return
        
        fig_pie = px.pie(
            values=list(status_data.values()),
            names=list(status_data.keys()),
            title="สถานะแรงดันโซลาร์"
        )
        fig_pie.update_layout(height=300)
        st.plotly_chart(fig_pie, width="stretch")
    
    with col2:
        # Display statistics
        st.subheader("สถิติแรงดันโซลาร์")
        st.write(f"**ค่าเฉลี่ย:** {metrics.get('avg_solar_voltage', 0):.2f} V")
        st.write(f"**ค่ามัธยฐาน:** {metrics.get('median_solar_voltage', 0):.2f} V")
        st.write(f"**ช่วง 25-75%:** {metrics.get('p25_solar_voltage', 0):.2f} - {metrics.get('p75_solar_voltage', 0):.2f} V")
        st.write(f"**ส่วนเบี่ยงเบนมาตรฐาน:** {metrics.get('std_solar_voltage', 0):.2f} V")
    
    # Main charts
    st.subheader("📈 กราฟวิเคราะห์การทำงานแผงโซลาร์")
    
    # Solar Performance vs Issues
    st.write("### 1. Solar Performance vs Station Issues (solar_volt_v เทียบกับ TIMEOUT/DISCONNECT)")
    perf_fig = create_solar_performance_scatter(df)
    if perf_fig:
        st.plotly_chart(perf_fig, width="stretch")
    
    # Solar-Battery Correlation
    st.write("### 2. Solar-Battery Correlation (solar_volt_v vs battery_v)")
    corr_fig = create_solar_battery_correlation(df)
    if corr_fig:
        st.plotly_chart(corr_fig, width="stretch")
    
    # Solar Voltage Daily Profile
    st.write("### 3. Solar Voltage Daily Profile (แรงดันโซลาร์รายวัน)")
    profile_fig = create_solar_daily_profile(df)
    if profile_fig:
        st.plotly_chart(profile_fig, width="stretch")
    
    # Under-charge Event Rate
    st.write("### 4. Under-charge Event Rate (จำนวนวันที่ Solar ต่ำ + Timeout สูง)")
    undercharge_fig = create_undercharge_event_rate(df)
    if undercharge_fig:
        st.plotly_chart(undercharge_fig, width="stretch")
    
    # Low Solar Stations Alert
    st.subheader("🚨 แจ้งเตือนสถานีที่มีปัญหาแผงโซลาร์")
    
    if not low_solar_stations.empty:
        st.markdown('<div class="critical-box">⚠️ <strong>คำเตือน:</strong> พบสถานีที่มีแรงดันโซลาร์ต่ำเป็นเวลาติดต่อง 3 วันขึ้นไป!</div>', unsafe_allow_html=True)
        
        # Format table
        display_df = low_solar_stations.copy()
        display_df['latest_solar_voltage'] = display_df['latest_solar_voltage'].round(2)
        display_df['latest_battery_voltage'] = display_df['latest_battery_voltage'].round(2)
        display_df['last_update'] = display_df['last_update'].dt.strftime('%Y-%m-%d %H:%M')
        
        display_df = display_df.rename(columns={
            'station_id': 'รหัสสถานี',
            'station_name': 'ชื่อสถานี',
            'consecutive_low_days': 'จำนวนวันติดต่อง',
            'latest_solar_voltage': 'แรงดันโซลาร์ล่าสุด (V)',
            'latest_battery_voltage': 'แรงดันแบตเตอรี่ล่าสุด (V)',
            'latest_status': 'สถานะล่าสุด',
            'last_update': 'อัปเดตล่าสุด'
        })
        
        st.dataframe(display_df, width="stretch", hide_index=True)
    else:
        st.markdown('<div class="success-box">✅ ไม่พบสถานีที่มีแรงดันโซลาร์ต่ำเป็นเวลาติดต่อง 3 วันขึ้นไป</div>', unsafe_allow_html=True)
    
    # Footer
    st.markdown("---")
    st.markdown('<p style="text-align: center; color: #666;">☀️ Solar Panel Dashboard - Real-time Monitoring System</p>', unsafe_allow_html=True)

if __name__ == "__main__":
    main()