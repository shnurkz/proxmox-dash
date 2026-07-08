import asyncio
import os
import time
import logging
from contextlib import asynccontextmanager
from typing import Dict, List, Tuple, Any

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Configuration from Environment Variables
PVE_API_URL = os.environ.get("PVE_API_URL")
PVE_TOKEN_ID = os.environ.get("PVE_TOKEN_ID", "netbox-sync@pve!netbox")
PVE_TOKEN_VALUE = os.environ.get("PVE_TOKEN_VALUE", "6751ad04-d06d-4612-af98-2740b928ad47")

# Verify configuration on startup
if not PVE_API_URL:
    logger.warning("PVE_API_URL environment variable is not set. Defaulting to dummy mock URL for initialization.")
    PVE_API_URL = "https://127.0.0.1:8006/api2/json"

# Proxmox API Authentication Headers
HEADERS = {
    "Authorization": f"PVEAPIToken={PVE_TOKEN_ID}={PVE_TOKEN_VALUE}",
    "Accept": "application/json"
}

# Cache Data Structure
class VMCache:
    def __init__(self, ttl: float = 3.0):
        self.ttl = ttl
        self.vms: List[Dict[str, Any]] = []
        self.stats: Dict[str, int] = {"total": 0, "running": 0, "stopped": 0, "with_agent": 0}
        self.last_updated: float = 0.0
        self.lock = asyncio.Lock()

# Global Cache instance
cache = VMCache(ttl=3.0)

# Lifecycle Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Establish connection pool (ignore self-signed SSL certs for local deployments)
    app.state.client = httpx.AsyncClient(verify=False, timeout=5.0)
    logger.info("FastAPI application started and client pool initialized.")
    yield
    # Clean up connection pool
    await app.state.client.aclose()
    logger.info("FastAPI application shutting down and client pool closed.")

# Initialize FastAPI with subpath configuration
app = FastAPI(
    title="Proxmox VE Monitor",
    root_path="/proxmox-dashboard",
    lifespan=lifespan
)

# Serve Local CSS and JS files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates Manager
templates = Jinja2Templates(directory="templates")


async def fetch_vm_ip(client: httpx.AsyncClient, node: str, vmid: int) -> str:
    """
    Fetches the first valid non-loopback IPv4 address of a VM using the QEMU Guest Agent.
    Gracefully returns 'No Agent' or 'N/A' on failures.
    """
    url = f"{PVE_API_URL.rstrip('/')}/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
    try:
        response = await client.get(url, headers=HEADERS, timeout=2.5)
        if response.status_code == 200:
            res_json = response.json()
            data = res_json.get("data", {})
            
            # Proxmox can return agent output either directly under 'data' or in a sub-field
            interfaces = []
            if isinstance(data, dict):
                interfaces = data.get("result", [])
            elif isinstance(data, list):
                interfaces = data

            for iface in interfaces:
                name = iface.get("name", "").lower()
                # Skip loopback interface
                if name in ("lo", "lo0") or "loopback" in name:
                    continue
                
                ip_addresses = iface.get("ip-addresses", [])
                for addr in ip_addresses:
                    if addr.get("ip-address-type") == "ipv4":
                        ip = addr.get("ip-address")
                        # Exclude localhost/loopback address blocks
                        if ip and not ip.startswith("127."):
                            return ip
                            
            return "No Agent"
        else:
            return "No Agent"
    except httpx.HTTPStatusError as e:
        logger.debug(f"Guest agent API returned HTTP status error for VM {vmid}: {e}")
        return "No Agent"
    except httpx.RequestError as e:
        logger.debug(f"Network error contacting guest agent for VM {vmid}: {e}")
        return "No Agent"
    except Exception as e:
        logger.debug(f"Unexpected error getting IP for VM {vmid}: {e}")
        return "No Agent"


async def fetch_proxmox_data(client: httpx.AsyncClient) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Fetches VM cluster resources and populates IP addresses concurrently.
    """
    url = f"{PVE_API_URL.rstrip('/')}/cluster/resources?type=vm"
    logger.info(f"Fetching VM resources from Proxmox Cluster API: {url}")
    
    response = await client.get(url, headers=HEADERS)
    response.raise_for_status()
    
    resources = response.json().get("data", [])
    
    vms = []
    for res in resources:
        # Filter cluster resources for virtual machines (typed 'qemu' in PVE resources)
        if res.get("type") == "qemu":
            tags_str = res.get("tags", "")
            
            # Clean and split tag string
            tags = []
            if tags_str:
                for sep in (',', ';', ' '):
                    if sep in tags_str:
                        tags = [t.strip() for t in tags_str.split(sep) if t.strip()]
                        break
                else:
                    tags = [tags_str.strip()] if tags_str.strip() else []

            vms.append({
                "vmid": res.get("vmid"),
                "name": res.get("name", "Unknown"),
                "status": res.get("status", "unknown"),
                "node": res.get("node", "unknown"),
                "cpu": res.get("cpu", 0.0),
                "mem": res.get("mem", 0),
                "maxmem": res.get("maxmem", 0),
                "tags_str": tags_str,
                "tags": tags
            })
            
    # Resolve IP addresses concurrently for all running VMs
    tasks = []
    running_vms = []
    for vm in vms:
        if vm["status"] == "running":
            running_vms.append(vm)
            tasks.append(fetch_vm_ip(client, vm["node"], vm["vmid"]))
        else:
            vm["ip"] = "N/A"
            
    if tasks:
        logger.info(f"Querying QEMU agent networks for {len(tasks)} running VMs concurrently.")
        ips = await asyncio.gather(*tasks)
        for vm, ip in zip(running_vms, ips):
            vm["ip"] = ip
            
    # Calculate stats
    total = len(vms)
    running = len(running_vms)
    stopped = total - running
    with_agent = sum(1 for vm in vms if vm["status"] == "running" and vm["ip"] not in ("No Agent", "N/A", ""))
    
    stats = {
        "total": total,
        "running": running,
        "stopped": stopped,
        "with_agent": with_agent
    }
    
    return vms, stats


async def get_cached_vms_and_stats(client: httpx.AsyncClient) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Serves data from the thread-safe 3-second cache if active, otherwise queries Proxmox.
    """
    async with cache.lock:
        now = time.time()
        if cache.vms and (now - cache.last_updated) < cache.ttl:
            logger.info("Serving VM details from in-memory cache.")
            return cache.vms, cache.stats
            
        logger.info("Cache expired or empty. Triggering live Proxmox refresh.")
        try:
            vms, stats = await fetch_proxmox_data(client)
            cache.vms = vms
            cache.stats = stats
            cache.last_updated = now
        except Exception as e:
            logger.error(f"Failed to fetch live Proxmox data: {e}")
            # Fall back to stale cache if API is offline
            if cache.vms:
                logger.warning("Returning stale cached data due to PVE API failure.")
                return cache.vms, cache.stats
            raise HTTPException(status_code=502, detail="Error fetching data from Proxmox cluster.")
            
        return cache.vms, cache.stats


@app.get("/", response_class=HTMLResponse)
async def read_dashboard(request: Request):
    """
    Renders the full HTML dashboard.
    """
    vms, stats = await get_cached_vms_and_stats(request.app.state.client)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"request": request, "vms": vms, "stats": stats}
    )


@app.get("/api/vms", name="api_vms", response_class=HTMLResponse)
async def api_vms(request: Request):
    """
    Renders the HTML partial for HTMX updates (table rows + stats OOB swaps).
    """
    vms, stats = await get_cached_vms_and_stats(request.app.state.client)
    return templates.TemplateResponse(
        request,
        "vms_partial.html",
        {"request": request, "vms": vms, "stats": stats}
    )
