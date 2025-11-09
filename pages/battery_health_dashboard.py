import streamlit as st
import pandas as pd
import json
import numpy as np
from datetime import datetime, timedelta
import pathlib
import warnings
warnings.filterwarnings('ignore')

# Set page configuration prefix for session state
PAGE_KEY_PREFIX = "battery_"

# Custom CSS for better styling
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        color: #1f77b4;
        text-align: center;
        margin-bottom: 2rem;
    }
    .metric-card {
        background-color: #f0f2f6;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 5px solid #1f77b4;
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
        
        # Filter out stations with missing battery data
        df = df.dropna(subset=['battery_v', 'timestamp'])
        
        return df
    
    except Exception as e:
        st.error(f"❌ โหลดข้อมูลล้มเหลว: {e}")
        return pd.DataFrame()

def calculate_battery_metrics(df):
    """คำนวณ metrics สำหรับ battery health"""
    if df.empty:
        return {}
    
    # Basic statistics
    metrics = {
        'total_stations': len(df),
        'avg_voltage': df['battery_v'].mean(),
        'min_voltage': df['battery_v'].min(),
        'max_voltage': df['battery_v'].max(),
        'std_voltage': df['battery_v'].std(),
        'median_voltage': df['battery_v'].median(),
        'p25_voltage': df['battery_v'].quantile(0.25),
        'p75_voltage': df['battery_v'].quantile(0.75)
    }
    
    # Count stations by voltage ranges
    metrics['critical_low'] = len(df[df['battery_v'] < 11.0])
    metrics['low'] = len(df[(df['battery_v'] >= 11.0) & (df['battery_v'] < 12.0)])
    metrics['normal'] = len(df[(df['battery_v'] >= 12.0) & (df['battery_v'] <= 14.0)])
    metrics['high'] = len(df[df['battery_v'] > 14.0])
    
    return metrics

def calculate_voltage_decay_rate(df):
    """คำนวณอัตราการลดลงของแรงดัน (ΔV/day)"""
    if df.empty:
        return pd.DataFrame()
    
    # Sort by station and timestamp
    df_sorted = df.sort_values(['station_id', 'timestamp'])
    
    decay_rates = []
    
    for station_id in df_sorted['station_id'].unique():
        station_data = df_sorted[df_sorted['station_id'] == station_id].copy()
        
        if len(station_data) < 2:
            continue
        
        # Calculate daily decay rate
        station_data = station_data.sort_values('timestamp')
        station_data['prev_voltage'] = station_data['battery_v'].shift(1)
        station_data['prev_timestamp'] = station_data['timestamp'].shift(1)
        
        # Calculate time difference in days
        station_data['days_diff'] = (station_data['timestamp'] - station_data['prev_timestamp']).dt.total_seconds() / (24 * 3600)
        
        # Calculate voltage decay rate (V/day)
        station_data['decay_rate'] = (station_data['prev_voltage'] - station_data['battery_v']) / station_data['days_diff']
        
        # Get the latest decay rate
        latest_decay = station_data.dropna(subset=['decay_rate']).iloc[-1] if not station_data['decay_rate'].dropna().empty else None
        
        if latest_decay is not None:
            decay_rates.append({
                'station_id': station_id,
                'station_name': station_data.iloc[-1].get('name_th', station_data.iloc[-1].get('name', 'Unknown')),
                'decay_rate': latest_decay['decay_rate'],
                'current_voltage': station_data.iloc[-1]['battery_v'],
                'last_update': station_data.iloc[-1]['timestamp']
            })
    
    return pd.DataFrame(decay_rates)

def detect_anomalies(df, threshold_std=2.0):
    """ตรวจจับค่าผิดปกติใน battery voltage"""
    if df.empty:
        return pd.DataFrame()
    
    anomalies = []
    
    for station_id in df['station_id'].unique():
        station_data = df[df['station_id'] == station_id].copy()
        
        if len(station_data) < 3:
            continue
        
        # Calculate rolling statistics
        station_data = station_data.sort_values('timestamp')
        station_data['rolling_mean'] = station_data['battery_v'].rolling(window=3, center=True).mean()
        station_data['rolling_std'] = station_data['battery_v'].rolling(window=3, center=True).std()
        
        # Detect anomalies (voltage drop > threshold_std * std)
        station_data['z_score'] = np.abs((station_data['battery_v'] - station_data['rolling_mean']) / station_data['rolling_std'])
        
        anomaly_points = station_data[station_data['z_score'] > threshold_std]
        
        for _, anomaly in anomaly_points.iterrows():
            anomalies.append({
                'station_id': station_id,
                'station_name': anomaly.get('name_th', anomaly.get('name', 'Unknown')),
                'timestamp': anomaly['timestamp'],
                'voltage': anomaly['battery_v'],
                'expected_voltage': anomaly['rolling_mean'],
                'z_score': anomaly['z_score'],
                'voltage_drop': anomaly['rolling_mean'] - anomaly['battery_v']
            })
    
    return pd.DataFrame(anomalies)

def create_battery_health_trend(df):
    """สร้างกราฟแนวโน้มสุขภาพแบตเตอรี่"""
    if df.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    # Group by date and calculate statistics
    df_daily = df.copy()
    df_daily['date'] = df_daily['timestamp'].dt.date
    
    daily_stats = df_daily.groupby('date').agg({
        'battery_v': ['median', 'mean', lambda x: x.quantile(0.25), lambda x: x.quantile(0.75)]
    }).reset_index()
    
    daily_stats.columns = ['date', 'median', 'mean', 'q25', 'q75']
    
    # Create line chart
    fig = go.Figure()
    
    # Add median line
    fig.add_trace(go.Scatter(
        x=daily_stats['date'],
        y=daily_stats['median'],
        mode='lines+markers',
        name='Median Voltage',
        line=dict(color='blue', width=3),
        marker=dict(size=6)
    ))
    
    # Add mean line
    fig.add_trace(go.Scatter(
        x=daily_stats['date'],
        y=daily_stats['mean'],
        mode='lines',
        name='Mean Voltage',
        line=dict(color='green', width=2, dash='dash')
    ))
    
    # Add confidence interval (25th-75th percentile)
    fig.add_trace(go.Scatter(
        x=daily_stats['date'],
        y=daily_stats['q75'],
        mode='lines',
        line=dict(width=0),
        showlegend=False,
        hoverinfo='skip'
    ))
    
    fig.add_trace(go.Scatter(
        x=daily_stats['date'],
        y=daily_stats['q25'],
        mode='lines',
        line=dict(width=0),
        fill='tonexty',
        fillcolor='rgba(0,100,80,0.2)',
        name='25th-75th Percentile',
        hoverinfo='skip'
    ))
    
    fig.update_layout(
        title='Battery Health Trend - Median & Percentile Analysis',
        xaxis_title='Date',
        yaxis_title='Battery Voltage (V)',
        hovermode='x unified',
        template='plotly_white',
        height=400
    )
    
    return fig

def create_decay_rate_ranking(decay_df):
    """สร้างกราฟจัดอันดับอัตราการเสื่อมสภาพแบตเตอรี่"""
    if decay_df.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.express as px
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    # Get top 15 stations with highest decay rate
    top_decay = decay_df.nlargest(15, 'decay_rate')
    
    # Create bar chart
    fig = px.bar(
        top_decay,
        x='decay_rate',
        y='station_id',
        orientation='h',
        title='Battery Decay Rate Ranking (ΔV/day)',
        labels={'decay_rate': 'Voltage Decay Rate (V/day)', 'station_id': 'Station ID'},
        color='decay_rate',
        color_continuous_scale='Reds'
    )
    
    fig.update_layout(
        yaxis={'categoryorder': 'total ascending'},
        height=500,
        template='plotly_white'
    )
    
    return fig

def create_anomaly_timeline(df, anomalies_df):
    """สร้างกราฟไทม์ไลน์พร้อมจุดผิดปกติ"""
    if df.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    fig = go.Figure()
    
    # Plot normal voltage timeline for each station
    for station_id in df['station_id'].unique()[:10]:  # Limit to 10 stations for clarity
        station_data = df[df['station_id'] == station_id].sort_values('timestamp')
        
        fig.add_trace(go.Scatter(
            x=station_data['timestamp'],
            y=station_data['battery_v'],
            mode='lines+markers',
            name=f'Station {station_id}',
            line=dict(width=2),
            marker=dict(size=4)
        ))
    
    # Add anomaly points
    if not anomalies_df.empty:
        fig.add_trace(go.Scatter(
            x=anomalies_df['timestamp'],
            y=anomalies_df['voltage'],
            mode='markers',
            name='Anomalies',
            marker=dict(
                size=10,
                color='red',
                symbol='x',
                line=dict(width=2, color='darkred')
            ),
            text=anomalies_df.apply(lambda x: f"Station: {x['station_id']}<br>Voltage Drop: {x['voltage_drop']:.2f}V", axis=1),
            hovertemplate='%{text}<extra></extra>'
        ))
    
    fig.update_layout(
        title='Battery Anomaly Timeline',
        xaxis_title='Timestamp',
        yaxis_title='Battery Voltage (V)',
        hovermode='closest',
        template='plotly_white',
        height=400
    )
    
    return fig

def create_voltage_distribution(df):
    """สร้าง boxplot การกระจายของแรงดันแบตเตอรี่"""
    if df.empty:
        return None
    
    # Lazy import plotly with error handling
    try:
        import plotly.graph_objects as go
    except Exception as e:
        st.error("ต้องติดตั้ง plotly เพื่อแสดงกราฟ: เพิ่ม 'plotly' ใน requirements แล้ว redeploy")
        return None
    
    # Create boxplot
    fig = go.Figure()
    
    fig.add_trace(go.Box(
        y=df['battery_v'],
        name='All Stations',
        boxpoints='outliers',
        jitter=0.3,
        pointpos=-1.8,
        marker_color='lightblue',
        line_color='darkblue'
    ))
    
    fig.update_layout(
        title='Battery Voltage Distribution Across All Stations',
        yaxis_title='Battery Voltage (V)',
        template='plotly_white',
        height=400
    )
    
    return fig

def main():
    """ฟังก์ชันหลักของ Dashboard"""
    st.title("🔋 Battery Health Dashboard")
    st.caption("พลังงานและสุขภาพของแบตเตอรี่สถานีวัดระดับน้ำฝน")
    
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
    
    # Voltage range filter
    voltage_range = st.sidebar.slider(
        "ช่วงแรงดันแบตเตอรี่ (V)",
        min_value=float(df['battery_v'].min()),
        max_value=float(df['battery_v'].max()),
        value=(float(df['battery_v'].min()), float(df['battery_v'].max()))
    )
    
    df = df[(df['battery_v'] >= voltage_range[0]) & (df['battery_v'] <= voltage_range[1])]
    
    # Calculate metrics
    metrics = calculate_battery_metrics(df)
    decay_rates = calculate_voltage_decay_rate(df)
    anomalies = detect_anomalies(df)
    
    # Display key metrics
    st.subheader("📊 ภาพรวมสุขภาพแบตเตอรี่")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("จำนวนสถานี", metrics.get('total_stations', 0))
    
    with col2:
        avg_v = metrics.get('avg_voltage', 0)
        st.metric("แรงดันเฉลี่ย", f"{avg_v:.2f} V")
    
    with col3:
        critical = metrics.get('critical_low', 0)
        st.metric("แรงดันต่ำวิกฤต", critical, delta=f"{critical} สถานี")
    
    with col4:
        normal = metrics.get('normal', 0)
        total = metrics.get('total_stations', 1)
        health_pct = (normal / total) * 100
        st.metric("สถานีปกติ", f"{health_pct:.1f}%")
    
    # Battery status distribution
    col1, col2 = st.columns(2)
    
    with col1:
        # Create pie chart for voltage status
        status_data = {
            'วิกฤตต่ำ (<11V)': metrics.get('critical_low', 0),
            'ต่ำ (11-12V)': metrics.get('low', 0),
            'ปกติ (12-14V)': metrics.get('normal', 0),
            'สูง (>14V)': metrics.get('high', 0)
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
            title="สถานะแรงดันแบตเตอรี่"
        )
        fig_pie.update_layout(height=300)
        st.plotly_chart(fig_pie, width="stretch")
    
    with col2:
        # Display statistics
        st.subheader("สถิติแรงดัน")
        st.write(f"**ค่าเฉลี่ย:** {metrics.get('avg_voltage', 0):.2f} V")
        st.write(f"**ค่ามัธยฐาน:** {metrics.get('median_voltage', 0):.2f} V")
        st.write(f"**ช่วง 25-75%:** {metrics.get('p25_voltage', 0):.2f} - {metrics.get('p75_voltage', 0):.2f} V")
        st.write(f"**ส่วนเบี่ยงเบนมาตรฐาน:** {metrics.get('std_voltage', 0):.2f} V")
    
    # Main charts
    st.subheader("📈 กราฟวิเคราะห์สุขภาพแบตเตอรี่")
    
    # Battery Health Trend
    st.write("### 1. Battery Health Trend (ค่า median/percentile ของ battery_v ตามเวลา)")
    trend_fig = create_battery_health_trend(df)
    if trend_fig:
        st.plotly_chart(trend_fig, width="stretch")
    
    # Battery Decay Rate Ranking
    st.write("### 2. Battery Decay Rate Ranking (แสดงสถานีที่ ΔV/day สูงสุด)")
    decay_fig = create_decay_rate_ranking(decay_rates)
    if decay_fig:
        st.plotly_chart(decay_fig, width="stretch")
    
    # Battery Anomaly Timeline
    st.write("### 3. Battery Anomaly Timeline (แรงดันตกเร็วผิดปกติ)")
    anomaly_fig = create_anomaly_timeline(df, anomalies)
    if anomaly_fig:
        st.plotly_chart(anomaly_fig, width="stretch")
    
    # Voltage Distribution
    st.write("### 4. Distribution ของ battery_v ทุกสถานี")
    dist_fig = create_voltage_distribution(df)
    if dist_fig:
        st.plotly_chart(dist_fig, width="stretch")
    
    # Top 10 stations with fastest voltage decay
    st.subheader("🏆 Top 10 สถานีที่แรงดันตกเร็วที่สุด")
    
    if not decay_rates.empty:
        top_10_decay = decay_rates.nlargest(10, 'decay_rate')
        
        # Format table
        display_df = top_10_decay.copy()
        display_df['decay_rate'] = display_df['decay_rate'].round(3)
        display_df['current_voltage'] = display_df['current_voltage'].round(2)
        display_df['last_update'] = display_df['last_update'].dt.strftime('%Y-%m-%d %H:%M')
        
        display_df = display_df.rename(columns={
            'station_id': 'รหัสสถานี',
            'station_name': 'ชื่อสถานี',
            'decay_rate': 'อัตราการลด (V/day)',
            'current_voltage': 'แรงดันปัจจุบัน (V)',
            'last_update': 'อัปเดตล่าสุด'
        })
        
        st.dataframe(display_df, width="stretch", hide_index=True)
        
        # Add warning for critical stations
        critical_stations = display_df[display_df['อัตราการลด (V/day)'] > 0.1]
        if not critical_stations.empty:
            st.markdown('<div class="warning-box">⚠️ <strong>คำเตือน:</strong> มีสถานีที่แรงดันตกเร็วผิดปกติ (>0.1 V/day) ควรตรวจสอบโดยเร่งด่วน!</div>', unsafe_allow_html=True)
    else:
        st.info("ไม่มีข้อมูลการเสื่อมสภาพของแบตเตอรี่")
    
    # Anomaly detection results
    if not anomalies.empty:
        st.subheader("🚨 ตรวจพบค่าผิดปกติ")
        
        # Show recent anomalies
        recent_anomalies = anomalies.nlargest(10, 'voltage_drop')
        
        for _, anomaly in recent_anomalies.iterrows():
            st.markdown(f"""
            <div class="warning-box">
                <strong>สถานี {anomaly['station_id']}</strong> - {anomaly['timestamp'].strftime('%Y-%m-%d %H:%M')}<br>
                แรงดัน: {anomaly['voltage']:.2f}V (คาดว่า: {anomaly['expected_voltage']:.2f}V)<br>
                การตกลง: {anomaly['voltage_drop']:.2f}V (Z-score: {anomaly['z_score']:.2f})
            </div>
            """, unsafe_allow_html=True)
    
    # Footer
    st.markdown("---")
    st.markdown('<p style="text-align: center; color: #666;">📊 Battery Health Dashboard - Real-time Monitoring System</p>', unsafe_allow_html=True)

if __name__ == "__main__":
    main()