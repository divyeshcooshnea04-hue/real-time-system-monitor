import dash
from dash import dcc, html, Input, Output
import dash_bootstrap_components as dbc
import plotly.graph_objs as go
import psutil
import datetime
from collections import deque
import os
import shutil
import threading
import time
from flask_socketio import SocketIO, emit
import eventlet
import signal
import sys

# ============ CONFIGURATION ============
MAX_HISTORY = 60
UPDATE_INTERVAL = 0.5  # 500ms for near real-time

# ============ GRACEFUL SHUTDOWN ============
shutdown_flag = False

def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    global shutdown_flag
    print("\n" + "="*50)
    print("👋 Received shutdown signal (Ctrl+C)")
    print("🔄 Cleaning up connections...")
    shutdown_flag = True
    # Give time for cleanup
    time.sleep(1)
    print("✅ Server shutting down gracefully.")
    print("👋 Goodbye!")
    print("="*50)
    sys.exit(0)

# Register signal handler
signal.signal(signal.SIGINT, signal_handler)

# ============ GET DISK USAGE ============
def get_disk_usage_percent():
    try:
        total, used, free = shutil.disk_usage("/")
        return (used / total) * 100
    except:
        try:
            if os.name == 'nt':
                partitions = psutil.disk_partitions()
                for partition in partitions:
                    if 'fixed' in partition.opts.lower() and 'c:' in partition.device.lower():
                        usage = psutil.disk_usage(partition.device)
                        return usage.percent
            return 0
        except:
            return 0

# ============ DATA STORAGE ============
class SystemData:
    def __init__(self):
        self.timestamps = deque(maxlen=MAX_HISTORY)
        self.cpu = deque(maxlen=MAX_HISTORY)
        self.ram = deque(maxlen=MAX_HISTORY)
        self.disk = deque(maxlen=MAX_HISTORY)
        self.network_sent = deque(maxlen=MAX_HISTORY)
        self.network_recv = deque(maxlen=MAX_HISTORY)
        self.processes = []
        self.battery = None
        
        self._initialize_data()
        
    def _initialize_data(self):
        now = datetime.datetime.now()
        for i in range(10):
            self.timestamps.append(now - datetime.timedelta(seconds=(10 - i)))
            self.cpu.append(psutil.cpu_percent(interval=0.1))
            self.ram.append(psutil.virtual_memory().percent)
            self.disk.append(get_disk_usage_percent())
            self.network_sent.append(0)
            self.network_recv.append(0)
        
    def update(self):
        """Collect current system metrics"""
        global shutdown_flag
        if shutdown_flag:
            return
            
        self.timestamps.append(datetime.datetime.now())
        self.cpu.append(psutil.cpu_percent(interval=0.1))
        self.ram.append(psutil.virtual_memory().percent)
        self.disk.append(get_disk_usage_percent())
        
        net_io = psutil.net_io_counters()
        self.network_sent.append(net_io.bytes_sent / 1024 / 1024)
        self.network_recv.append(net_io.bytes_recv / 1024 / 1024)
        
        # Get top processes
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                proc_info = proc.info
                proc_info['cpu_percent'] = proc.cpu_percent(interval=0.1)
                processes.append(proc_info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self.processes = sorted(processes, key=lambda x: x.get('cpu_percent', 0), reverse=True)[:10]
        
        # Battery
        self.battery = psutil.sensors_battery()

# Initialize data store
system_data = SystemData()

# Get drive letter
def get_display_drive():
    if os.name == 'nt':
        return os.environ.get('SystemDrive', 'C:')
    return '/'

DISPLAY_DRIVE = get_display_drive()

# ============ DASH APP ============
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.DARKLY])
app.title = "Real-Time System Monitor"

# Enable WebSocket
socketio = SocketIO(app.server, cors_allowed_origins="*", async_mode='eventlet')

# ============ BACKGROUND THREAD ============
def background_data_collector():
    """Background thread to collect and emit data"""
    global shutdown_flag
    while not shutdown_flag:
        system_data.update()
        
        # Prepare data for emission
        data = {
            'cpu': list(system_data.cpu),
            'ram': list(system_data.ram),
            'disk': list(system_data.disk),
            'timestamps': [t.isoformat() for t in list(system_data.timestamps)],
            'network_sent': list(system_data.network_sent),
            'network_recv': list(system_data.network_recv),
            'processes': system_data.processes[:5],
            'battery': system_data.battery.percent if system_data.battery else None,
            'battery_plugged': system_data.battery.power_plugged if system_data.battery else None,
            'ram_percent': system_data.ram[-1],
            'cpu_percent': system_data.cpu[-1],
            'disk_percent': system_data.disk[-1],
            'process_count': len(psutil.pids()),
            'timestamp': datetime.datetime.now().strftime("%H:%M:%S")
        }
        
        # Emit to all connected clients
        try:
            socketio.emit('system_update', data)
        except:
            pass  # Silently handle if socket is closed
        
        eventlet.sleep(UPDATE_INTERVAL)
    
    print("🔄 Background collector stopped.")

# Start background thread
thread = threading.Thread(target=background_data_collector, daemon=True)
thread.start()

# ============ LAYOUT ============
app.layout = dbc.Container([
    # Header with real-time badge
    dbc.Row([
        dbc.Col(html.H1([
            "🖥️ Real-Time System Monitor",
            html.Span(" 🔴 LIVE", className="text-danger", style={'fontSize': '20px'})
        ], className="text-center text-primary mb-4 mt-3"), width=12)
    ]),
    
    # Quick Stats Cards
    dbc.Row([
        dbc.Col(dbc.Card([
            dbc.CardBody([
                html.H5("CPU Usage", className="card-title"),
                html.H2(id="cpu-current", children="0%", className="text-info"),
                html.Small("Real-time: ", className="text-muted"),
                html.Small(id="last-update", children="...")
            ])
        ], color="info", inverse=True), width=3),
        
        dbc.Col(dbc.Card([
            dbc.CardBody([
                html.H5("RAM Usage", className="card-title"),
                html.H2(id="ram-current", children="0%", className="text-success"),
                html.Small(f"Total: {psutil.virtual_memory().total // (1024**3)} GB")
            ])
        ], color="success", inverse=True), width=3),
        
        dbc.Col(dbc.Card([
            dbc.CardBody([
                html.H5("Disk Usage", className="card-title"),
                html.H2(id="disk-current", children="0%", className="text-warning"),
                html.Small(f"Drive: {DISPLAY_DRIVE}")
            ])
        ], color="warning", inverse=True), width=3),
        
        dbc.Col(dbc.Card([
            dbc.CardBody([
                html.H5("Processes", className="card-title"),
                html.H2(id="process-count", children="0", className="text-danger"),
                html.Small("Running processes")
            ])
        ], color="danger", inverse=True), width=3),
    ], className="mb-4"),
    
    # Battery Status
    dbc.Row([
        dbc.Col(html.Div(id="battery-status"), width=12)
    ], className="mb-3"),
    
    # Charts
    dbc.Row([
        dbc.Col(dbc.Card([
            dbc.CardBody([
                dcc.Graph(id="cpu-graph", config={'displayModeBar': False})
            ])
        ]), width=6),
        
        dbc.Col(dbc.Card([
            dbc.CardBody([
                dcc.Graph(id="ram-graph", config={'displayModeBar': False})
            ])
        ]), width=6),
    ], className="mb-4"),
    
    dbc.Row([
        dbc.Col(dbc.Card([
            dbc.CardBody([
                dcc.Graph(id="disk-graph", config={'displayModeBar': False})
            ])
        ]), width=6),
        
        dbc.Col(dbc.Card([
            dbc.CardBody([
                dcc.Graph(id="network-graph", config={'displayModeBar': False})
            ])
        ]), width=6),
    ], className="mb-4"),
    
    # Process Tables
    dbc.Row([
        dbc.Col(dbc.Card([
            dbc.CardHeader("🔥 Top 5 CPU-Consuming Processes"),
            dbc.CardBody([
                html.Div(id="cpu-process-table")
            ])
        ]), width=6),
        
        dbc.Col(dbc.Card([
            dbc.CardHeader("💾 Top 5 Memory-Consuming Processes"),
            dbc.CardBody([
                html.Div(id="memory-process-table")
            ])
        ]), width=6),
    ], className="mb-4"),
    
    # Status Footer
    dbc.Row([
        dbc.Col(html.Div([
            html.Hr(),
            html.P("🟢 System running smoothly", className="text-success text-center"),
            html.Small("Press Ctrl+C to stop the server", className="text-muted text-center d-block")
        ], className="text-center"), width=12)
    ]),
    
    # WebSocket client connection
    dcc.Store(id='websocket-data', data={}),
    dcc.Interval(
        id='interval-component',
        interval=500,  # 500ms for UI updates
        n_intervals=0
    )
], fluid=True, className="p-4")

# ============ WEBSOCKET CLIENT SCRIPT ============
app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.5.1/socket.io.min.js"></script>
        <script>
            var socket = io.connect(window.location.origin);
            socket.on('connect', function() {
                console.log('🔗 WebSocket connected!');
                document.title = '✅ Real-Time System Monitor';
            });
            socket.on('disconnect', function() {
                console.log('🔌 WebSocket disconnected');
                document.title = '❌ System Monitor - Disconnected';
            });
            socket.on('system_update', function(data) {
                console.log('📊 Data received:', data.timestamp);
                window.latestData = data;
                var event = new CustomEvent('systemUpdate', { detail: data });
                document.dispatchEvent(event);
            });
        </script>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
'''

# ============ CALLBACKS ============
@app.callback(
    [Output("cpu-current", "children"),
     Output("ram-current", "children"),
     Output("disk-current", "children"),
     Output("process-count", "children"),
     Output("last-update", "children"),
     Output("battery-status", "children")],
    [Input("interval-component", "n_intervals")]
)
def update_stats(n):
    """Update stats from WebSocket data"""
    data = system_data
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    
    cpu = f"{data.cpu[-1]:.1f}%"
    ram = f"{data.ram[-1]:.1f}%"
    disk = f"{data.disk[-1]:.1f}%"
    processes = len(psutil.pids())
    
    # Battery
    battery_html = ""
    battery = data.battery
    if battery:
        percent = battery.percent
        plugged = battery.power_plugged
        status = "Plugged in" if plugged else "Discharging"
        icon = "⚡" if percent < 20 else "🔋"
        color = "danger" if percent < 20 else "success"
        battery_html = dbc.Alert(f"{icon} Battery: {percent}% ({status})", color=color)
    
    return cpu, ram, disk, processes, timestamp, battery_html

@app.callback(
    [Output("cpu-graph", "figure"),
     Output("ram-graph", "figure"),
     Output("disk-graph", "figure"),
     Output("network-graph", "figure")],
    [Input("interval-component", "n_intervals")]
)
def update_graphs(n):
    """Update all graphs"""
    timestamps = list(system_data.timestamps)
    cpu_data = list(system_data.cpu)
    ram_data = list(system_data.ram)
    disk_data = list(system_data.disk)
    net_sent = list(system_data.network_sent)
    net_recv = list(system_data.network_recv)
    
    time_str = [t.strftime("%H:%M:%S") for t in timestamps]
    
    # CPU Graph
    cpu_fig = go.Figure()
    cpu_fig.add_trace(go.Scatter(
        x=time_str, y=cpu_data, mode='lines+markers',
        name='CPU Usage', line=dict(color='#17a2b8', width=3),
        marker=dict(size=6, color='#17a2b8')
    ))
    cpu_fig.update_layout(
        title='CPU Usage Over Time (WebSocket)',
        xaxis_title='Time', yaxis_title='Usage (%)',
        yaxis_range=[0, 100], template='plotly_dark',
        height=300, margin=dict(l=40, r=20, t=40, b=30),
        showlegend=True, hovermode='x unified'
    )
    
    # RAM Graph
    ram_fig = go.Figure()
    ram_fig.add_trace(go.Scatter(
        x=time_str, y=ram_data, mode='lines+markers',
        name='RAM Usage', line=dict(color='#28a745', width=3),
        marker=dict(size=6, color='#28a745')
    ))
    ram_fig.add_hline(y=90, line_dash="dash", line_color="red", annotation_text="⚠️ Warning")
    ram_fig.update_layout(
        title='RAM Usage Over Time (WebSocket)',
        xaxis_title='Time', yaxis_title='Usage (%)',
        yaxis_range=[0, 100], template='plotly_dark',
        height=300, margin=dict(l=40, r=20, t=40, b=30),
        showlegend=True, hovermode='x unified'
    )
    
    # Disk Graph
    disk_fig = go.Figure()
    disk_fig.add_trace(go.Scatter(
        x=time_str, y=disk_data, mode='lines+markers',
        name='Disk Usage', line=dict(color='#ffc107', width=3),
        marker=dict(size=6, color='#ffc107')
    ))
    disk_fig.update_layout(
        title=f'Disk Usage Over Time ({DISPLAY_DRIVE})',
        xaxis_title='Time', yaxis_title='Usage (%)',
        yaxis_range=[0, 100], template='plotly_dark',
        height=300, margin=dict(l=40, r=20, t=40, b=30),
        showlegend=True, hovermode='x unified'
    )
    
    # Network Graph
    net_fig = go.Figure()
    net_fig.add_trace(go.Scatter(
        x=time_str, y=net_sent, mode='lines+markers',
        name='📤 Upload (MB)', line=dict(color='#fd7e14', width=2),
        marker=dict(size=4, color='#fd7e14')
    ))
    net_fig.add_trace(go.Scatter(
        x=time_str, y=net_recv, mode='lines+markers',
        name='📥 Download (MB)', line=dict(color='#20c997', width=2),
        marker=dict(size=4, color='#20c997')
    ))
    net_fig.update_layout(
        title='Network Activity (WebSocket)',
        xaxis_title='Time', yaxis_title='Data (MB)',
        template='plotly_dark', height=300,
        margin=dict(l=40, r=20, t=40, b=30),
        showlegend=True, hovermode='x unified'
    )
    
    return cpu_fig, ram_fig, disk_fig, net_fig

@app.callback(
    Output("cpu-process-table", "children"),
    [Input("interval-component", "n_intervals")]
)
def update_cpu_processes(n):
    """Show top 5 CPU-consuming processes"""
    processes = system_data.processes[:5]
    
    table_header = html.Thead(html.Tr([
        html.Th("PID", style={'color': '#17a2b8'}),
        html.Th("Process Name", style={'color': '#17a2b8'}),
        html.Th("CPU %", style={'color': '#17a2b8'}),
        html.Th("Memory %", style={'color': '#17a2b8'})
    ]))
    
    table_rows = []
    for proc in processes:
        cpu = f"{proc.get('cpu_percent', 0):.1f}%"
        mem = f"{proc.get('memory_percent', 0):.1f}%"
        table_rows.append(html.Tr([
            html.Td(proc.get('pid', 'N/A'), style={'color': '#ffffff'}),
            html.Td(proc.get('name', 'Unknown'), style={'color': '#ffffff'}),
            html.Td(cpu, className="text-info", style={'font-weight': 'bold'}),
            html.Td(mem, className="text-success", style={'font-weight': 'bold'})
        ]))
    
    return dbc.Table([table_header, html.Tbody(table_rows)], bordered=True, 
                     hover=True, striped=True, size="sm", className="mt-2",
                     style={'backgroundColor': '#2d2d2d'})

@app.callback(
    Output("memory-process-table", "children"),
    [Input("interval-component", "n_intervals")]
)
def update_memory_processes(n):
    """Show top 5 Memory-consuming processes"""
    processes = sorted(system_data.processes, key=lambda x: x.get('memory_percent', 0), reverse=True)[:5]
    
    table_header = html.Thead(html.Tr([
        html.Th("PID", style={'color': '#28a745'}),
        html.Th("Process Name", style={'color': '#28a745'}),
        html.Th("Memory %", style={'color': '#28a745'}),
        html.Th("CPU %", style={'color': '#28a745'})
    ]))
    
    table_rows = []
    for proc in processes:
        mem = f"{proc.get('memory_percent', 0):.1f}%"
        cpu = f"{proc.get('cpu_percent', 0):.1f}%"
        table_rows.append(html.Tr([
            html.Td(proc.get('pid', 'N/A'), style={'color': '#ffffff'}),
            html.Td(proc.get('name', 'Unknown'), style={'color': '#ffffff'}),
            html.Td(mem, className="text-success", style={'font-weight': 'bold'}),
            html.Td(cpu, className="text-info", style={'font-weight': 'bold'})
        ]))
    
    return dbc.Table([table_header, html.Tbody(table_rows)], bordered=True, 
                     hover=True, striped=True, size="sm", className="mt-2",
                     style={'backgroundColor': '#2d2d2d'})

# ============ RUN APP ============
if __name__ == "__main__":
    print("="*50)
    print("🚀 Starting Real-Time System Monitor with WebSocket...")
    print(f"📊 Monitoring drive: {DISPLAY_DRIVE}")
    print("📊 Open http://127.0.0.1:8050 in your browser")
    print("⚡ Real-time updates via WebSocket (0.5s interval)")
    print("🔴 WebSocket server running...")
    print("="*50)
    print("\n💡 Press Ctrl+C to stop the server gracefully\n")
    
    try:
        socketio.run(app.server, debug=True, host="127.0.0.1", port=8050, use_reloader=False)
    except KeyboardInterrupt:
        print("\n" + "="*50)
        print("👋 Received shutdown signal (Ctrl+C)")
        print("🔄 Cleaning up connections...")
        # Give the background thread time to clean up
        time.sleep(0.5)
        print("✅ Server shut down gracefully.")
        print("👋 Goodbye!")
        print("="*50)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("🔄 Attempting graceful shutdown...")