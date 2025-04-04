import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import matplotlib
matplotlib.use("Agg")  # Use non-interactive backend
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)

# Chart storage configuration
CHARTS_DIR = Path(os.path.expanduser("~/Desktop/tasty_charts"))
CHARTS_DIR.mkdir(parents=True, exist_ok=True)

# In-memory store for chart metadata
charts: Dict[str, Dict] = {}

# Initialize FastAPI app
app = FastAPI(title="TastyAgent Chart Server")

# Serve static files (charts)
app.mount("/charts", StaticFiles(directory=str(CHARTS_DIR)), name="charts")

class ChartData(BaseModel):
    """Model for chart data"""
    title: str
    x_data: list
    y_data: list
    x_label: str = "Date"
    y_label: str = "Value"
    chart_type: str = "line"  # line, bar, scatter, etc.
    time_back: Optional[str] = None

@app.get("/", response_class=HTMLResponse)
async def index():
    """Return a simple index page listing available charts"""
    charts_list = [f'<li><a href="/view/{id}">{data["title"]}</a> - {data["created_at"]}</li>' 
                   for id, data in charts.items()]

    return f"""
    <html>
        <head><title>TastyAgent Charts</title></head>
        <body>
            <h1>TastyAgent Charts</h1>
            <ul>{"".join(charts_list) if charts_list else "<li>No charts available</li>"}</ul>
        </body>
    </html>
    """

@app.get("/view/{chart_id}", response_class=HTMLResponse)
async def view_chart(chart_id: str):
    """View a specific chart with some metadata"""
    if chart_id not in charts:
        raise HTTPException(status_code=404, detail="Chart not found")
        
    data = charts[chart_id]
    return f"""
    <html>
        <head><title>{data["title"]}</title></head>
        <body>
            <h1>{data["title"]}</h1>
            <p>Created: {data["created_at"]} | 
               Type: {data.get("chart_type", "Line")} |
               {f'Period: {data["time_back"]}' if "time_back" in data else ""}</p>
            <img src="/charts/{os.path.basename(data["path"])}" style="width: 100%; max-width: 900px;">
            <p><a href="/">Back to all charts</a></p>
        </body>
    </html>
    """

@app.post("/api/charts/")
async def create_chart(chart_data: ChartData):
    """Create a new chart from the provided data"""
    try:
        chart_id = str(uuid.uuid4())
        
        # Create chart
        plt.figure(figsize=(10, 6))
        if chart_data.chart_type == "line":
            plt.plot(chart_data.x_data, chart_data.y_data, 'b-')
        elif chart_data.chart_type == "bar":
            plt.bar(chart_data.x_data, chart_data.y_data)
        elif chart_data.chart_type == "scatter":
            plt.scatter(chart_data.x_data, chart_data.y_data)
            
        plt.title(chart_data.title)
        plt.xlabel(chart_data.x_label)
        plt.ylabel(chart_data.y_label)
        plt.grid(True)
        
        # Save to file
        filename = f"{chart_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        filepath = CHARTS_DIR / filename
        plt.savefig(filepath)
        plt.close()
        
        # Store metadata
        charts[chart_id] = {
            "id": chart_id,
            "title": chart_data.title,
            "path": str(filepath),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "chart_type": chart_data.chart_type
        }
        
        if chart_data.time_back:
            charts[chart_id]["time_back"] = chart_data.time_back
        
        return {"id": chart_id, "message": "Chart created successfully"}
        
    except Exception as e:
        logger.error(f"Error creating chart: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/charts/{chart_id}/image")
async def get_chart_image(chart_id: str):
    """Get the image file for a specific chart"""
    if chart_id not in charts:
        raise HTTPException(status_code=404, detail="Chart not found")
    
    return FileResponse(charts[chart_id]["path"])

# Server instance and management
chart_server = None
server_task = None

async def start_server(host="127.0.0.1", port=8000):
    """Start the FastAPI server in the background"""
    global chart_server, server_task
    
    if server_task and not server_task.done():
        return f"http://{host}:{port}"  # Already running
    
    config = uvicorn.Config(app, host=host, port=port, log_level="error", 
                           access_log=False, log_config=None)
    chart_server = uvicorn.Server(config)
    server_task = asyncio.create_task(chart_server.serve())
    
    return f"http://{host}:{port}"

async def create_nlv_chart(history_data, time_back):
    """Create an NLV chart from history data and return the URL"""
    try:
        # Start server if needed
        server_url = await start_server()
        
        # Extract data
        x_data = [n.time for n in history_data]
        y_data = [float(n.close) for n in history_data]
        
        # Create chart
        result = await create_chart(ChartData(
            title=f"Portfolio Value History (Past {time_back})",
            x_data=x_data, 
            y_data=y_data,
            x_label="Date",
            y_label="Portfolio Value ($)",
            chart_type="line",
            time_back=time_back
        ))
        
        return f"{server_url}/view/{result['id']}"
    except Exception as e:
        logger.error(f"Error creating chart: {e}")
        raise e