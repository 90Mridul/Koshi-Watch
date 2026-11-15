"""Python runtime for Koshi Watch. Run with: python app.py"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import hashlib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory
from processing import channel_metrics, compute_bbox_overlap_ratio, extract_channel_lines, rainfall_summary

ROOT = Path(__file__).resolve().parent
# Vercel's deployment filesystem is read-only except for /tmp. Local runs keep
# their database and caches in the project data/ directory as before.
DATA = Path(os.environ.get("KOSHI_DATA_DIR", "/tmp/koshi-watch" if os.environ.get("VERCEL") else ROOT / "data"))
CACHE = DATA / "tile_cache"
for folder in (DATA, CACHE / "basemap", CACHE / "imagery", CACHE / "jrc", CACHE / "sat", CACHE / "gibs", CACHE / "nisar"):
    folder.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "koshi_watch.db"
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
TITILER = "https://planetarycomputer.microsoft.com/api/data/v1"
app = Flask(__name__, static_folder=None)
IMAGE_WARMER = ThreadPoolExecutor(max_workers=2, thread_name_prefix='koshi-image-cache')
WARMING_IMAGES = set()
WARMING_LOCK = threading.Lock()
RIVER_CACHE_WARMER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='koshi-river-cache')
RIVER_CACHE_STATE = {'running': False, 'phase': 'idle', 'year': None, 'completed': 0, 'total': 0, 'error': None}
RIVER_CACHE_STATE_LOCK = threading.Lock()

REGIONS = [
    ("koshi-tappu", "Koshi Tappu Ramsar Wetland", "Wetland & Braided Floodplain", "Protected Ramsar floodplain wetland upstream of Koshi Barrage, supporting seasonal marshes, braided sandbars, and wild water buffalo habitat.", 87.005, 26.645, 11.4, [86.94,26.56,87.07,26.72]),
    ("koshi-barrage", "Koshi Barrage Ponding Reach", "Hydraulic Control & Flood Gate", "56-gate barrage at Bhimnagar regulating Sapta Koshi discharge onto the Terai alluvial fan.", 86.935,26.525,11.6,[86.88,26.47,87.0,26.57]),
    ("kusaha-breach", "Kusaha 2008 Avulsion Corridor", "Embankment Breach Zone", "Eastern embankment breach site in Sunsari, Nepal where the Koshi jumped eastward into an older paleochannel.",87.025,26.555,11.2,[86.94,26.44,87.09,26.64]),
    ("chatara-gorge", "Chatara Gorge Fan Apex", "Himalayan Mountain Exit", "Confluence of the Sun Koshi, Arun, and Tamor rivers exiting the Mahabharat Range onto the Terai alluvial plain.",87.145,26.845,11.4,[87.09,26.78,87.21,26.9]),
]
STATIONS = [{"id":"koshi-barrage","name":"Koshi Barrage (Bhimnagar)","latitude":26.525,"longitude":86.935,"warningThresholdM3s":4250,"dangerThresholdM3s":8500,"description":"Primary transboundary barrage regulating flow from Nepal into Bihar."},{"id":"chatara-gorge","name":"Chatara Gorge Outlet","latitude":26.865,"longitude":87.152,"warningThresholdM3s":4000,"dangerThresholdM3s":8000,"description":"Mountain outlet downstream of Sun Koshi, Arun, and Tamor confluence."}]
LANDMARKS = [{"id":"chatara-gorge","name":"Chatara Gorge","role":"Mountain exit onto Terai alluvial fan","lat":26.858,"lng":87.152,"zoom":12.2},{"id":"koshi-tappu","name":"Koshi Tappu Reserve","role":"Ramsar braided floodplain wetland","lat":26.645,"lng":87.005,"zoom":11.4},{"id":"kusaha-breach","name":"Kusaha 2008 Breach Site","role":"Aug 2008 eastern embankment failure","lat":26.585,"lng":87.024,"zoom":12.0},{"id":"koshi-barrage","name":"Koshi Barrage (Bhimnagar)","role":"56-gate hydraulic flood control structure","lat":26.525,"lng":86.925,"zoom":12.2}]
MILESTONES = [{"id":"milestone-1736-1950","era":"1736–1950","title":"~113 km Westward Megafan Migration","summary":"Historical maps document episodic westward migration of the Sapta Koshi megafan.","source":"Inglis (1941); Gole & Chitale (1966); Wells & Dorr (1987)"},{"id":"milestone-1959-barrage","era":"1959–1963","title":"Koshi Barrage & Dual Embankment Confinement","summary":"Construction of the Koshi Barrage and embankments confined the river corridor.","source":"Chakraborty et al. (2010)"},{"id":"milestone-1988-landsat","era":"1988–2007","title":"Satellite Record Begins (Braided Channel Migration)","summary":"Landsat captures channel migration and braiding upstream of Koshi Barrage.","source":"USGS Landsat C2-L2 / EC JRC Surface Water","recommendedSatYear":1995},{"id":"milestone-2008-kusaha","era":"Aug 2008","title":"Kusaha Eastern Embankment Avulsion","summary":"The river breached the eastern embankment at Kusaha on 18 August 2008.","source":"NASA MODIS & Landsat 2008 Observations","recommendedSatYear":2008},{"id":"milestone-2016-present","era":"2016–Present","title":"10 m Sentinel-2 Monitoring","summary":"Sentinel-2 tracks fine-scale braid switching inside Koshi Tappu.","source":"ESA Sentinel-2 L2A (Planetary Computer)","recommendedSatYear":2024}]

def connect():
    c=sqlite3.connect(DB_PATH, timeout=20); c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE IF NOT EXISTS regions(id TEXT PRIMARY KEY,name TEXT,category TEXT,description TEXT,center_lng REAL,center_lat REAL,zoom REAL,bbox_json TEXT,geojson_json TEXT,area_km2 REAL); CREATE TABLE IF NOT EXISTS water_analyses(id INTEGER PRIMARY KEY AUTOINCREMENT,region_id TEXT,scene_id TEXT,observation_date TEXT,cloud_cover REAL,ndwi_threshold REAL,total_area_km2 REAL,water_area_km2 REAL,water_fraction REAL,mean_ndwi REAL,valid_pixels INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(region_id,scene_id,ndwi_threshold)); CREATE TABLE IF NOT EXISTS api_cache(cache_key TEXT PRIMARY KEY,payload_json TEXT,expires_at REAL);''')
    for rid,name,cat,desc,lng,lat,zoom,bbox in REGIONS:
        coords=[[[bbox[0],bbox[1]],[bbox[2],bbox[1]],[bbox[2],bbox[3]],[bbox[0],bbox[3]],[bbox[0],bbox[1]]]]
        geo={"type":"Feature","geometry":{"type":"Polygon","coordinates":coords},"properties":{"id":rid,"name":name}}
        # Spherical polygon area, sufficient to match the source project's area estimates.
        ring=coords[0]; area=0.0
        for i in range(len(ring)-1):
            lon1,lat1=map(math.radians,ring[i]); lon2,lat2=map(math.radians,ring[i+1]); area+=(lon2-lon1)*(2+math.sin(lat1)+math.sin(lat2))
        km2=abs(area)*6371008.8**2/2/1e6
        c.execute('INSERT INTO regions VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,category=excluded.category,description=excluded.description,center_lng=excluded.center_lng,center_lat=excluded.center_lat,zoom=excluded.zoom,bbox_json=excluded.bbox_json,geojson_json=excluded.geojson_json,area_km2=excluded.area_km2',(rid,name,cat,desc,lng,lat,zoom,json.dumps(bbox),json.dumps(geo),round(km2,4)))
    c.commit(); return c

def region(row):
    d=dict(row); d['bbox']=json.loads(d.pop('bbox_json')); d['geojson']=json.loads(d.pop('geojson_json')); return d
def cached(key):
    c=connect(); row=c.execute('SELECT payload_json,expires_at FROM api_cache WHERE cache_key=?',(key,)).fetchone(); c.close()
    if row:
        expiry=float(row['expires_at']); expiry=expiry/1000 if expiry>100_000_000_000 else expiry
        if expiry>datetime.now().timestamp(): return json.loads(row['payload_json'])
    return None
def cache(key,value,ttl):
    c=connect(); c.execute('INSERT OR REPLACE INTO api_cache VALUES(?,?,?)',(key,json.dumps(value),datetime.now().timestamp()+ttl)); c.commit(); c.close()
def fetch_json(url, **kwargs):
    res=requests.get(url,timeout=15,**kwargs); res.raise_for_status(); return res.json()
def feature_region(r): return {k:r[k] for k in ('id','name','category','description','center_lng','center_lat','zoom','bbox','geojson','area_km2')}

def _water_analysis_source(scene_id):
    """Return the Planetary Computer collection and NDWI expression for a STAC item ID."""
    if scene_id.startswith(('LC08_', 'LC09_', 'LE07_', 'LT04_', 'LT05_')):
        return 'landsat-c2-l2', '(green-nir08)/(green%2Bnir08)'
    if scene_id.startswith(('S2A_', 'S2B_', 'S2C_')):
        return 'sentinel-2-l2a', '(B03-B08)/(B03%2BB08)'
    raise ValueError('Unsupported scene ID. Choose a Landsat Collection 2 or Sentinel-2 L2A scene.')

def _satellite_cache_path(col,item,mode,bbox,w,h):
    # Match the TypeScript cache key, whose bbox tuple is joined with commas.
    signature=f'{col}|{item}|{mode}|{",".join(map(str,bbox))}|{w}x{h}|v3'
    return CACHE/'sat'/(hashlib.sha256(signature.encode()).hexdigest()+'.png')

def _render_satellite_image(col,item,mode,bbox,w,h):
    path=_satellite_cache_path(col,item,mode,bbox,w,h)
    if path.exists() and path.stat().st_size>512: return path
    if col=='sentinel-2-l2a': q=('expression=(B03-B08)/(B03%2BB08)&asset_as_band=true&rescale=-0.4,0.6&colormap_name=rdbu' if mode=='ndwi' else 'assets=visual&asset_bidx=visual%7C1,2,3&nodata=0')
    else: q=('expression=(green-nir08)/(green%2Bnir08)&asset_as_band=true&rescale=-0.4,0.6&colormap_name=rdbu' if mode=='ndwi' else 'assets=red&assets=green&assets=blue&color_formula=Gamma+RGB+1.8+Saturation+1.2+Sigmoidal+RGB+15+0.35')
    response=requests.get(f"{TITILER}/item/bbox/{','.join(map(str,bbox))}/{w}x{h}.png?collection={col}&item={quote(item)}&{q}&coord_crs=epsg:4326&dst_crs=epsg:3857",timeout=25)
    response.raise_for_status()
    if len(response.content)<512: raise ValueError('Empty satellite image returned by TiTiler.')
    temp=path.with_name(path.name+f'.tmp-{threading.get_ident()}'); temp.write_bytes(response.content); os.replace(temp,path)
    return path

def _warm_image(url):
    try:
        query=parse_qs(urlsplit(url).query); col=query['collection'][0]; item=query['item'][0]; mode=query.get('mode',['visual'])[0]
        bbox=[float(x) for x in query['bbox'][0].split(',')]; w=min(1400,max(256,int(query.get('w',['960'])[0]))); h=min(1400,max(256,int(query.get('h',['1152'])[0])))
        if col not in ('sentinel-2-l2a','landsat-c2-l2') or mode not in ('visual','ndwi') or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',item): return
        if len(bbox)!=4 or bbox[0]<86.5 or bbox[1]<26.2 or bbox[2]>87.45 or bbox[3]>27.05 or bbox[0]>=bbox[2] or bbox[1]>=bbox[3]: return
        _render_satellite_image(col,item,mode,bbox,w,h)
    except (KeyError,ValueError,requests.RequestException):
        pass
    finally:
        with WARMING_LOCK: WARMING_IMAGES.discard(url)

def _schedule_scene_precache(scenes):
    """Warm the newest scene alternatives in the background; the request stays nonblocking."""
    try: scene_count=max(0,min(16,int(os.environ.get('SATELLITE_PRECACHE_SCENES','4'))))
    except ValueError: scene_count=4
    image_urls=[]
    for scene in scenes[:scene_count]:
        for name in ('visualImageUrl','ndwiImageUrl','companionVisualImageUrl','companionNdwiImageUrl'):
            if scene.get(name): image_urls.append(scene[name])
    for url in dict.fromkeys(image_urls):
        # Avoid duplicate work when both the client and warm-up need the same PNG.
        with WARMING_LOCK:
            if url in WARMING_IMAGES: continue
            WARMING_IMAGES.add(url)
        IMAGE_WARMER.submit(_warm_image,url)

def _warm_river_years():
    """Cache river imagery newest-to-oldest; visual true color is the first full pass."""
    current=date.today().year
    years=['latest', *range(current-1,1987,-1)]
    total=len(years)*2
    with RIVER_CACHE_STATE_LOCK:
        RIVER_CACHE_STATE.update(running=True,phase='Sentinel true color first',year=None,completed=0,total=total,error=None)
    observations={}
    try:
        for mode in ('visual','ndwi'):
            for year in years:
                with RIVER_CACHE_STATE_LOCK:
                    RIVER_CACHE_STATE.update(phase='True color' if mode=='visual' else 'Water index',year=year)
                try:
                    with app.test_request_context('/api/rivers/satellite-year',query_string={'year':str(year),'includeCourse':'false'}):
                        response=satellite_year()
                        payload=response.get_json() if response.status_code==200 else None
                    observation=(payload or {}).get('observation')
                    if observation:
                        observations[year]=observation
                        keys=('visualImageUrl','companionVisualImageUrl') if mode=='visual' else ('ndwiImageUrl','companionNdwiImageUrl')
                        for key in keys:
                            if observation.get(key): _warm_image(observation[key])
                except Exception as exc:
                    with RIVER_CACHE_STATE_LOCK:
                        RIVER_CACHE_STATE['error']=str(exc)[:240]
                finally:
                    with RIVER_CACHE_STATE_LOCK: RIVER_CACHE_STATE['completed']+=1
        with RIVER_CACHE_STATE_LOCK:
            RIVER_CACHE_STATE.update(running=False,phase='complete',year=None)
    except Exception as exc:
        with RIVER_CACHE_STATE_LOCK:
            RIVER_CACHE_STATE.update(running=False,phase='failed',error=str(exc)[:240])

def _start_river_cache_warmup():
    with RIVER_CACHE_STATE_LOCK:
        if RIVER_CACHE_STATE['running'] or RIVER_CACHE_STATE['phase']=='complete': return
        RIVER_CACHE_STATE['running']=True
    RIVER_CACHE_WARMER.submit(_warm_river_years)

def _cache_sar_browse(url):
    """Cache ASF SAR browse quicklooks; never download full science granules."""
    parsed=urlsplit(url) if isinstance(url,str) else None
    nisar=bool(parsed and parsed.scheme=='https' and parsed.hostname=='nisar.asf.earthdatacloud.nasa.gov' and parsed.path.startswith('/BROWSE/') and (parsed.path.endswith('_LATLON.png') or parsed.path.endswith('_LATLON_thumbnail.png')))
    sentinel=bool(parsed and parsed.scheme=='https' and parsed.hostname=='datapool.asf.alaska.edu' and parsed.path.startswith('/BROWSE/') and parsed.path.endswith('.jpg'))
    if not (nisar or sentinel): raise ValueError('Unsupported ASF SAR browse URL.')
    extension='.png' if nisar else '.jpg'
    path=CACHE/'nisar'/(hashlib.sha256(url.encode()).hexdigest()+extension)
    if path.exists() and path.stat().st_size>128: return path
    response=requests.get(url,timeout=25); response.raise_for_status()
    magic=response.content[:8] if nisar else response.content[:3]
    valid_magic=b'\x89PNG\r\n\x1a\n' if nisar else b'\xff\xd8\xff'
    if magic!=valid_magic or len(response.content)<128 or len(response.content)>30_000_000:
        raise ValueError('ASF returned an invalid SAR browse image.')
    temp=path.with_name(path.name+f'.tmp-{threading.get_ident()}'); temp.write_bytes(response.content); os.replace(temp,path)
    return path

def _warm_sar_browse(url):
    try: _cache_sar_browse(url)
    except (ValueError,requests.RequestException): pass

def _nisar_overlay_corners(kml_url):
    parsed=urlsplit(kml_url)
    if not (parsed.scheme=='https' and parsed.hostname=='nisar.asf.earthdatacloud.nasa.gov' and parsed.path.startswith('/BROWSE/') and parsed.path.endswith('_LATLON.kml')):
        raise ValueError('Unsupported NISAR georeference URL.')
    key='nisar-georef:v1:'+hashlib.sha256(kml_url.encode()).hexdigest(); saved=cached(key)
    if saved is not None: return saved['coordinates']
    response=requests.get(kml_url,timeout=20); response.raise_for_status()
    if len(response.content)>250_000: raise ValueError('Invalid NISAR overlay metadata size.')
    root=ET.fromstring(response.content)
    text_node=next((element.text for element in root.iter() if element.tag.endswith('coordinates') and element.text),None)
    if not text_node: raise ValueError('NISAR browse metadata has no ground corners.')
    corners=[]
    for pair in text_node.strip().split():
        values=[float(x) for x in pair.split(',')[:2]]
        if len(values)!=2 or not all(math.isfinite(x) for x in values) or not(-180<=values[0]<=180 and -90<=values[1]<=90):
            raise ValueError('Invalid NISAR ground corner coordinates.')
        corners.append(values)
    if len(corners)!=4: raise ValueError('NISAR browse metadata does not define four corners.')
    coordinates=[corners[3],corners[2],corners[1],corners[0]]
    cache(key,{'coordinates':coordinates},30*86400)
    return coordinates

def _mask_nisar_overlay(url, geometry):
    """Make ASF's full-resolution, lat/lon browse transparent outside its actual scene footprint."""
    parsed=urlsplit(url)
    if not (parsed.scheme=='https' and parsed.hostname=='nisar.asf.earthdatacloud.nasa.gov' and parsed.path.startswith('/BROWSE/') and parsed.path.endswith('_LATLON.png')):
        raise ValueError('Unsupported NISAR overlay image URL.')
    if not isinstance(geometry,dict) or geometry.get('type') not in ('Polygon','MultiPolygon'):
        raise ValueError('NISAR overlay footprint must be a polygon.')
    encoded=json.dumps(geometry,separators=(',',':'))
    if len(encoded)>30_000: raise ValueError('NISAR footprint is too large.')
    footprint=geometry.get('coordinates')
    polys=[footprint] if geometry['type']=='Polygon' else footprint
    if not isinstance(polys,list) or not polys: raise ValueError('NISAR footprint is empty.')
    try: coords=_nisar_overlay_corners(url[:-4]+'.kml')
    except (requests.RequestException,ET.ParseError,ValueError) as exc: raise ValueError(f'Cannot read NISAR image georeference: {exc}') from exc
    signature=hashlib.sha256((url+'|'+encoded).encode()).hexdigest(); folder=CACHE/'nisar'/'overlay'; folder.mkdir(parents=True,exist_ok=True); output=folder/(signature+'.png')
    if output.exists() and output.stat().st_size>512: return output
    from PIL import Image,ImageDraw
    source=_cache_sar_browse(url)
    with Image.open(source) as opened:
        image=opened.convert('RGBA'); width,height=image.size
    p0,p1,p2,p3=coords
    def pixel(lon,lat):
        # Invert MapLibre/KML's bilinear quadrilateral map to its raster pixel coordinates.
        u=v=.5
        for _ in range(12):
            ax=(1-u)*(1-v)*p0[0]+u*(1-v)*p1[0]+u*v*p2[0]+(1-u)*v*p3[0]
            ay=(1-u)*(1-v)*p0[1]+u*(1-v)*p1[1]+u*v*p2[1]+(1-u)*v*p3[1]
            dux=(1-v)*(p1[0]-p0[0])+v*(p2[0]-p3[0]); duy=(1-v)*(p1[1]-p0[1])+v*(p2[1]-p3[1])
            dvx=(1-u)*(p3[0]-p0[0])+u*(p2[0]-p1[0]); dvy=(1-u)*(p3[1]-p0[1])+u*(p2[1]-p1[1])
            det=dux*dvy-duy*dvx
            if abs(det)<1e-12: break
            ex=ax-lon; ey=ay-lat; step_u=(ex*dvy-ey*dvx)/det; step_v=(dux*ey-duy*ex)/det
            u-=step_u; v-=step_v
            if abs(step_u)+abs(step_v)<1e-8: break
        return (round(u*(width-1)),round(v*(height-1)))
    mask=Image.new('L',(width,height),0); painter=ImageDraw.Draw(mask)
    for polygon in polys:
        if not isinstance(polygon,list) or not polygon: continue
        rings=polygon
        painter.polygon([pixel(float(point[0]),float(point[1])) for point in rings[0] if len(point)>=2],fill=255)
        for hole in rings[1:]: painter.polygon([pixel(float(point[0]),float(point[1])) for point in hole if len(point)>=2],fill=0)
    image.putalpha(mask)
    temp=output.with_name(output.name+f'.tmp-{threading.get_ident()}'); image.save(temp,format='PNG',optimize=True); os.replace(temp,output)
    return output

def _warm_nisar_overlay(url, geometry):
    try: _mask_nisar_overlay(url,geometry)
    except (ValueError,requests.RequestException,ET.ParseError): pass

@app.get('/api/sar/scenes')
def sar_scenes():
    c=connect(); row=c.execute('SELECT * FROM regions WHERE id=?',(request.args.get('regionId','koshi-tappu'),)).fetchone(); c.close()
    if not row: return jsonify(error='Unknown regionId'),400
    r=region(row); acquisition_date=request.args.get('date',date.today().isoformat()); mission=request.args.get('mission','both')
    try: date.fromisoformat(acquisition_date)
    except ValueError: return jsonify(error='Dates must use YYYY-MM-DD.'),400
    if acquisition_date>date.today().isoformat(): return jsonify(error='date cannot be in the future.'),400
    if mission not in ('both','nisar','sentinel-1'): return jsonify(error='mission must be both, nisar, or sentinel-1.'),400
    try: limit=min(50,max(1,int(request.args.get('limit',20))))
    except ValueError: return jsonify(error='limit must be an integer.'),400
    include_nearby=request.args.get('includeNearby','0')=='1'
    key=f"asf-sar:v4:{r['id']}:{acquisition_date}:{mission}:{limit}:{int(include_nearby)}"; saved=cached(key)
    if saved is not None:
        for scene in saved.get('scenes',[])[:2]:
            params=parse_qs(urlsplit(scene.get('overlayImageUrl') or scene['browseUrl']).query)
            source=params.get('url',[''])[0]
            if source and scene.get('mission')=='NISAR' and params.get('footprint'):
                try: IMAGE_WARMER.submit(_warm_nisar_overlay,source,json.loads(params['footprint'][0]))
                except (ValueError,TypeError): pass
            elif source: IMAGE_WARMER.submit(_warm_sar_browse,source)
        return jsonify(saved)
    bbox=r['bbox']
    try:
        sources=[('NISAR','GCOV')] if mission=='nisar' else [('SENTINEL-1','GRD_HD,GRD_MD,GRD_FD')] if mission=='sentinel-1' else [('NISAR','GCOV'),('SENTINEL-1','GRD_HD,GRD_MD,GRD_FD')]
        scenes=[]; warm_urls=[]
        for dataset,products in sources:
            if dataset=='NISAR' and acquisition_date<'2026-06-17': continue
            if dataset=='SENTINEL-1' and acquisition_date<'2014-10-03': continue
            response=requests.get('https://api.daac.asf.alaska.edu/services/search/param',params={
                'dataset':dataset,'processingLevel':products,'bbox':','.join(map(str,bbox)),
                'start':acquisition_date+'T00:00:00Z','end':acquisition_date+'T23:59:59Z','output':'geojson','maxResults':limit,
            },timeout=25); response.raise_for_status()
            for feature in response.json().get('features',[]):
                props=feature.get('properties') or {}; scene_id=props.get('fileID') or props.get('sceneName')
                browse_list=props.get('browse') or []
                if dataset=='NISAR': browse=next((u for u in browse_list if isinstance(u,str) and u.startswith('https://nisar.asf.earthdatacloud.nasa.gov/BROWSE/') and u.endswith('_LATLON_thumbnail.png')),None)
                else: browse=next((u for u in browse_list if isinstance(u,str) and u.startswith('https://datapool.asf.alaska.edu/BROWSE/') and u.endswith('.jpg')),None)
                if not scene_id or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}',scene_id) or not browse: continue
                overlay_image=next((u for u in browse_list if isinstance(u,str) and u.startswith('https://nisar.asf.earthdatacloud.nasa.gov/BROWSE/') and u.endswith('_LATLON.png')),None) if dataset=='NISAR' else None
                kml_url=next((u for u in (props.get('additionalUrls') or []) if isinstance(u,str) and u.startswith('https://nisar.asf.earthdatacloud.nasa.gov/BROWSE/') and u.endswith('_LATLON.kml')),None) if dataset=='NISAR' else None
                geometry=feature.get('geometry')
                overlay_route='/api/sar/overlay?url='+quote(overlay_image,safe='')+'&footprint='+quote(json.dumps(geometry,separators=(',',':')),safe='') if overlay_image and geometry else None
                product=props.get('processingLevel') or ('GCOV' if dataset=='NISAR' else 'GRD')
                scenes.append({'id':scene_id,'date':(props.get('startTime') or '')[:10], 'startTime':props.get('startTime'),
                    'stopTime':props.get('stopTime'),'mission':'NISAR' if dataset=='NISAR' else 'Sentinel-1',
                    'product':product,'direction':props.get('flightDirection'),'orbit':props.get('orbit'),
                    'polarization':props.get('mainBandPolarization') or props.get('polarization'),
                    'geometry':geometry,'browseUrl':'/api/sar/browse?url='+quote(browse,safe=''),
                    'overlayImageUrl':overlay_route,
                    'overlayMetadataUrl':'/api/sar/georef?url='+quote(kml_url,safe='') if kml_url else None,
                    'archiveUrl':'https://search.asf.alaska.edu/#/?dataset='+quote(dataset,safe='')+'&granule_list='+quote(scene_id,safe='')})
                warm_urls.append((overlay_image,geometry) if overlay_route else browse)
        scenes.sort(key=lambda item:item.get('startTime') or '',reverse=True)
        result={'regionId':r['id'],'date':acquisition_date,'mission':mission,'scenes':scenes[:limit]}
        if include_nearby and not scenes:
            requested_day=date.fromisoformat(acquisition_date); first=(requested_day-timedelta(days=15)).isoformat(); last=(requested_day+timedelta(days=15)).isoformat(); nearby=set()
            for dataset,products in sources:
                if dataset=='NISAR' and last<'2026-06-17': continue
                if dataset=='SENTINEL-1' and last<'2014-10-03': continue
                try:
                    nearby_response=requests.get('https://api.daac.asf.alaska.edu/services/search/param',params={
                        'dataset':dataset,'processingLevel':products,'bbox':','.join(map(str,bbox)),
                        'start':first+'T00:00:00Z','end':last+'T23:59:59Z','output':'geojson','maxResults':50,
                    },timeout=20); nearby_response.raise_for_status()
                    for feature in nearby_response.json().get('features',[]):
                        observed=(feature.get('properties') or {}).get('startTime','')[:10]
                        if observed and observed!=acquisition_date: nearby.add(observed)
                except requests.RequestException: pass
            result['nearbyDates']=sorted(nearby,key=lambda value:(abs((date.fromisoformat(value)-requested_day).days),value))[:5]
        cache(key,result,21600)
        for item in warm_urls[:2]:
            if isinstance(item,tuple): IMAGE_WARMER.submit(_warm_nisar_overlay,item[0],item[1])
            else: IMAGE_WARMER.submit(_warm_sar_browse,item)
        return jsonify(result)
    except (requests.RequestException,ValueError,KeyError) as exc:
        return jsonify(error=f'SAR archive unavailable: {exc}'),502

@app.get('/api/sar/browse')
def sar_browse():
    url=request.args.get('url','')
    try:
        path=_cache_sar_browse(url); return send_file(path,mimetype='image/png' if path.suffix=='.png' else 'image/jpeg',conditional=True)
    except (requests.RequestException,ValueError) as exc: return jsonify(error=f'SAR quicklook unavailable: {exc}'),502

@app.get('/api/sar/dates')
def sar_dates():
    c=connect(); row=c.execute('SELECT * FROM regions WHERE id=?',(request.args.get('regionId','koshi-tappu'),)).fetchone(); c.close()
    if not row: return jsonify(error='Unknown regionId'),400
    r=region(row); mission=request.args.get('mission','nisar')
    if mission not in ('both','nisar','sentinel-1'): return jsonify(error='mission must be both, nisar, or sentinel-1.'),400
    lower=request.args.get('startDate','2026-06-17' if mission=='nisar' else '2014-10-03')
    upper=request.args.get('endDate',(date.today()-timedelta(days=1)).isoformat())
    try: lower_day=date.fromisoformat(lower); upper_day=date.fromisoformat(upper)
    except ValueError: return jsonify(error='Dates must use YYYY-MM-DD.'),400
    if lower_day>upper_day or upper_day>=date.today(): return jsonify(error='The SAR date range must end by yesterday and start before its end.'),400
    key=f"asf-sar-dates:v1:{r['id']}:{mission}:{lower}:{upper}"; saved=cached(key)
    if saved is not None: return jsonify(saved)
    sources=[('NISAR','GCOV','2026-06-17')] if mission=='nisar' else [('SENTINEL-1','GRD_HD,GRD_MD,GRD_FD','2014-10-03')] if mission=='sentinel-1' else [('NISAR','GCOV','2026-06-17'),('SENTINEL-1','GRD_HD,GRD_MD,GRD_FD','2014-10-03')]
    dates=set()
    try:
        for dataset,products,available_from in sources:
            start=max(lower,available_from)
            if start>upper: continue
            response=requests.get('https://api.daac.asf.alaska.edu/services/search/param',params={
                'dataset':dataset,'processingLevel':products,'bbox':','.join(map(str,r['bbox'])),
                'start':start+'T00:00:00Z','end':upper+'T23:59:59Z','output':'geojson','maxResults':5000,
            },timeout=30); response.raise_for_status()
            for feature in response.json().get('features',[]):
                observed=(feature.get('properties') or {}).get('startTime','')[:10]
                if observed:
                    try:
                        if lower<=observed<=upper: dates.add(date.fromisoformat(observed).isoformat())
                    except ValueError: pass
        result={'regionId':r['id'],'mission':mission,'startDate':lower,'endDate':upper,'dates':sorted(dates)}
        cache(key,result,21600); return jsonify(result)
    except (requests.RequestException,ValueError,TypeError) as exc:
        return jsonify(error=f'SAR acquisition-date list unavailable: {exc}'),502

@app.get('/api/sar/overlay')
def sar_overlay():
    try:
        geometry=json.loads(request.args.get('footprint',''))
        path=_mask_nisar_overlay(request.args.get('url',''),geometry)
        return send_file(path,mimetype='image/png',conditional=True)
    except (requests.RequestException,ValueError,TypeError,KeyError,IndexError) as exc:
        return jsonify(error=f'NISAR map overlay unavailable: {exc}'),502

@app.get('/api/sar/georef')
def sar_georef():
    url=request.args.get('url',''); parsed=urlsplit(url)
    if not (parsed.scheme=='https' and parsed.hostname=='nisar.asf.earthdatacloud.nasa.gov' and parsed.path.startswith('/BROWSE/') and parsed.path.endswith('_LATLON.kml')):
        return jsonify(error='Unsupported NISAR georeference URL.'),400
    try:
        return jsonify(coordinates=_nisar_overlay_corners(url))
    except (requests.RequestException,ET.ParseError,ValueError) as exc:
        return jsonify(error=f'NISAR georeference unavailable: {exc}'),502

@app.get('/api/health')
def health():
    c=connect(); counts=[c.execute('SELECT count(*) FROM regions').fetchone()[0],c.execute('SELECT count(*) FROM water_analyses').fetchone()[0]]; c.close()
    return jsonify(status='ok',regionsSeeded=counts[0],savedWaterAnalyses=counts[1],timestamp=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'))

@app.get('/api/observatory/regions')
def regions():
    c=connect(); rows=[feature_region(region(r)) for r in c.execute('SELECT * FROM regions')]; c.close(); return jsonify(regions=rows)

@app.get('/api/rivers/channels')
def channels():
    _start_river_cache_warmup()
    key='curated-courses:v3'; courses=cached(key)
    if courses is None:
        from concurrent.futures import ThreadPoolExecutor
        years=[1995,2001,2008,2016,2021,'latest']
        def get_course(year):
            try:
                with app.test_request_context('/api/rivers/satellite-year',query_string={'year':str(year),'includeCourse':'true'}):
                    response=satellite_year()
                    if response.status_code!=200: return None
                    return response.get_json().get('course')
            except Exception: return None
        with ThreadPoolExecutor(max_workers=6) as pool: courses=[c for c in pool.map(get_course,years) if c]
        # Match the source's baseline-relative lateral displacement and ascending year order.
        courses.sort(key=lambda c:c['actualYear'])
        if courses:
            baseline=courses[0]['mainstem']
            for i,item in enumerate(courses):
                item['maxShiftKm']=0 if i==0 else channel_metrics(item['mainstem'],baseline)['maxLateralShiftKm']
            unique={item['actualYear']:item for item in courses}; courses=sorted(unique.values(),key=lambda c:c['actualYear']); cache(key,courses,21600)
    return jsonify(courses=courses,milestones=MILESTONES,landmarks=LANDMARKS,stations=STATIONS)

@app.get('/api/rivers/cache-status')
def river_cache_status():
    with RIVER_CACHE_STATE_LOCK: state=dict(RIVER_CACHE_STATE)
    state['percent']=round(100*state['completed']/state['total']) if state['total'] else 0
    return jsonify(state)

@app.get('/api/satellite/scenes')
def scenes():
    c=connect(); row=c.execute('SELECT * FROM regions WHERE id=?',(request.args.get('regionId','koshi-tappu'),)).fetchone(); c.close()
    if not row: return jsonify(error='Unknown regionId'),400
    r=region(row); end=request.args.get('endDate',date.today().isoformat()); start=request.args.get('startDate',(date.today()-timedelta(days=180)).isoformat())
    try: date.fromisoformat(end); date.fromisoformat(start)
    except ValueError: return jsonify(error='Dates must use YYYY-MM-DD.'),400
    if start>end: return jsonify(error='startDate must be earlier than or equal to endDate.'),400
    provider=request.args.get('provider','sentinel-2-l2a')
    if provider not in ('auto','sentinel-2-l2a','landsat-c2-l2'): return jsonify(error='provider must be auto, sentinel-2-l2a, or landsat-c2-l2.'),400
    try: cloud=min(100,max(1,float(request.args.get('maxCloud',35)))); limit=min(30,max(1,int(request.args.get('limit',15))))
    except ValueError: return jsonify(error='maxCloud and limit must be numeric.'),400
    cache_provider=provider
    key=f"stac:v2:{r['id']}:{start}:{end}:{cloud}:{limit}" if provider=='sentinel-2-l2a' else f"stac:v2:{r['id']}:{start}:{end}:{cloud}:{limit}:{provider}"
    saved=cached(key)
    if saved is not None:
        # The original TypeScript cache stores only the scene list; the HTTP route wraps it.
        if isinstance(saved,list): saved=dict(regionId=r['id'],regionName=r['name'],startDate=start,endDate=end,maxCloudCover=cloud,provider=provider,scenes=saved)
        _schedule_scene_precache(saved.get('scenes',[]))
        return jsonify(saved)
    try:
        cutoff='2015-06-23'
        collections=([provider] if provider!='auto' else (['landsat-c2-l2'] if end<cutoff else ['sentinel-2-l2a'] if start>=cutoff else ['landsat-c2-l2','sentinel-2-l2a']))
        feature_rows=[]
        for collection in collections:
            payload={"collections":[collection],"bbox":r['bbox'],"datetime":f"{start}T00:00:00Z/{end}T23:59:59Z","query":{"eo:cloud_cover":{"lt":cloud}},"limit":40,"sortby":[{"field":"properties.datetime","direction":"desc"}]}
            resp=requests.post(STAC,json=payload,timeout=20); resp.raise_for_status()
            feature_rows.extend((feature,collection) for feature in resp.json().get('features',[]))
        feature_rows.sort(key=lambda item:item[0].get('properties',{}).get('datetime',''),reverse=True)
        out=[]; seen=set()
        for f,collection in feature_rows:
            b=f.get('bbox')
            if not b or len(b)<4: continue
            overlap=compute_bbox_overlap_ratio(b,r['bbox'])
            if overlap<.75: continue
            props=f.get('properties',{}); dt=props.get('datetime',''); d=dt[:10]
            if collection=='sentinel-2-l2a': tile=props.get('s2:mgrs_tile') or f.get('id','').split('_')[3] if len(f.get('id','').split('_'))>3 else props.get('s2:mgrs_tile','unknown')
            else: tile=f"{props.get('landsat:wrs_path','')}-{props.get('landsat:wrs_row','')}" or f.get('id','')
            dedup=f"{collection}:{d}:{tile}"
            if dedup in seen: continue
            seen.add(dedup); ib=[max(b[0],86.72),max(b[1],26.36),min(b[2],87.22),min(b[3],26.92)]
            aspect=max(.01,(ib[2]-ib[0])*math.cos(math.radians((ib[1]+ib[3])/2)))/max(.01,ib[3]-ib[1])
            if aspect >= 1: w,h=1152,max(256,round(1152/aspect))
            else: w,h=max(256,round(1152*aspect)),1152
            bp=quote(','.join(map(str,ib))); item=quote(f['id'],safe='')
            base=f"/api/satellite/image?collection={collection}&item={item}&bbox={bp}&w={w}&h={h}"
            if collection=='sentinel-2-l2a':
                preview=f.get('assets',{}).get('rendered_preview',{}).get('href') or f'{TITILER}/item/preview.png?collection={collection}&item={item}&assets=visual&asset_bidx=visual%7C1,2,3&nodata=0&format=png'
                sensor=f"ESA Sentinel-2 ({f.get('id','')[:3]}, 10 m)"
            else:
                preview=f.get('assets',{}).get('rendered_preview',{}).get('href') or f'{TITILER}/item/preview.png?collection={collection}&item={item}&assets=red&assets=green&assets=blue&format=png'
                scene_id=f.get('id','')
                sensor='Landsat 9 OLI-2 (30 m)' if scene_id.startswith('LC09') else 'Landsat 8 OLI (30 m)' if scene_id.startswith('LC08') else 'Landsat 7 ETM+ (30 m)' if scene_id.startswith('LE07') else 'Landsat 5 TM (30 m)' if scene_id.startswith('LT05') else 'USGS Landsat C2-L2 (30 m)'
            scene=dict(id=f['id'],datetime=dt,date=d,cloudCover=round(float(props.get('eo:cloud_cover') or 0),2),mgrsTile=str(tile),collection=collection,sensorLabel=sensor,bbox=b,imageBounds=ib,overlapRatio=round(overlap,2),previewUrl=preview,visualImageUrl=base+'&mode=visual',ndwiImageUrl=base+'&mode=ndwi')
            peers=[(peer,peer_collection) for peer,peer_collection in feature_rows if peer.get('id')!=f.get('id') and peer_collection==collection and peer.get('properties',{}).get('datetime','')[:10]==d]
            if peers:
                peer,peer_collection=peers[0]; pb=peer.get('bbox'); pib=[max(pb[0],86.72),max(pb[1],26.36),min(pb[2],87.22),min(pb[3],26.92)]
                if pib[0]<pib[2] and pib[1]<pib[3]:
                    pa=(pib[2]-pib[0])*math.cos(math.radians((pib[1]+pib[3])/2))/(pib[3]-pib[1]); pw,ph=(1152,max(256,round(1152/pa))) if pa>=1 else (max(256,round(1152*pa)),1152); pbase=f'/api/satellite/image?collection={collection}&item={quote(peer["id"],safe="")}&bbox={quote(",".join(map(str,pib)))}&w={pw}&h={ph}'
                    scene.update(companionVisualImageUrl=pbase+'&mode=visual',companionNdwiImageUrl=pbase+'&mode=ndwi',companionImageBounds=pib)
            out.append(scene)
            if len(out)>=limit: break
        result=dict(regionId=r['id'],regionName=r['name'],startDate=start,endDate=end,maxCloudCover=cloud,provider=provider,scenes=out); cache(key,result,21600); _schedule_scene_precache(out); return jsonify(result)
    except requests.RequestException as e: return jsonify(error=f'Satellite scene service unavailable: {e}'),502

@app.get('/api/satellite/image')
def satellite_image():
    col=request.args.get('collection',''); item=request.args.get('item',''); mode=request.args.get('mode','visual')
    if col not in ('sentinel-2-l2a','landsat-c2-l2') or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',item) or mode not in ('visual','ndwi'): return jsonify(error='Invalid collection, item, or mode.'),400
    try:
        bbox=list(map(float,request.args.get('bbox','').split(','))); w=min(1400,max(256,int(request.args.get('w',960)))); h=min(1400,max(256,int(request.args.get('h',1152))))
        if len(bbox)!=4 or bbox[0]<86.5 or bbox[1]<26.2 or bbox[2]>87.45 or bbox[3]>27.05 or bbox[0]>=bbox[2] or bbox[1]>=bbox[3]: raise ValueError()
    except ValueError: return jsonify(error='Invalid or out-of-range bbox or dimensions.'),400
    try:
        path=_render_satellite_image(col,item,mode,bbox,w,h); return send_file(path,mimetype='image/png',max_age=604800)
    except (requests.RequestException,ValueError) as e: return jsonify(error=str(e)),502

@app.get('/api/analysis/saved')
def saved_analyses():
    c=connect(); rows=[dict(x) for x in c.execute('SELECT w.*,r.name AS region_name FROM water_analyses w JOIN regions r ON r.id=w.region_id ORDER BY observation_date DESC,created_at DESC LIMIT 50')]; c.close(); return jsonify(records=rows)

@app.post('/api/analysis/water-area')
def water_area():
    body=request.get_json(silent=True) or {}; rid=body.get('regionId'); scene=body.get('sceneId')
    if not isinstance(rid,str) or not isinstance(scene,str) or not scene.strip(): return jsonify(error='regionId and sceneId are required.'),400
    c=connect(); row=c.execute('SELECT * FROM regions WHERE id=?',(rid,)).fetchone()
    if not row: c.close(); return jsonify(error=f'Region not found: {rid}'),404
    r=region(row); threshold=body.get('ndwiThreshold',0)
    if not isinstance(threshold,(int,float)) or not -.5<=threshold<=.5: c.close(); return jsonify(error='ndwiThreshold must be between -0.5 and 0.5.'),400
    threshold=round(float(threshold),2); found=c.execute('SELECT * FROM water_analyses WHERE region_id=? AND scene_id=? AND ndwi_threshold=?',(rid,scene.strip(),threshold)).fetchone()
    if found:
        x=dict(found); c.close(); frac=x['water_fraction']; total=x['total_area_km2']; return jsonify(analysis=dict(regionId=rid,regionName=r['name'],sceneId=x['scene_id'],observationDate=x['observation_date'],cloudCover=x['cloud_cover'],ndwiThreshold=threshold,totalPolygonAreaKm2=total,waterAreaKm2=x['water_area_km2'],landAreaKm2=round(total-x['water_area_km2'],2),waterFraction=frac,validPixelCount=x['valid_pixels'],waterPixelCount=round(x['valid_pixels']*frac),meanNdwi=x['mean_ndwi'],medianNdwi=x['mean_ndwi'],minNdwi=-1,maxNdwi=1,cached=True))
    c.close()
    try:
        scene_id=scene.strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',scene_id):
            return jsonify(error='Invalid scene ID.'),400
        try: collection,expression=_water_analysis_source(scene_id)
        except ValueError as e: return jsonify(error=str(e)),400
        url=f'{TITILER}/item/statistics?collection={collection}&item={quote(scene_id,safe="")}&expression={expression}&asset_as_band=true&histogram_bins=24'
        response=requests.post(url,json=r['geojson'],timeout=(5,30)); response.raise_for_status()
        payload=response.json(); statistics=payload.get('properties',{}).get('statistics',{})
        if not isinstance(statistics,dict) or not statistics:
            raise ValueError('The imagery service returned no NDWI statistics for this scene and region.')
        stats=next(iter(statistics.values())); histogram=stats.get('histogram')
        if not isinstance(histogram,list) or len(histogram)!=2 or not histogram[0] or len(histogram[1])!=len(histogram[0])+1:
            raise ValueError('The imagery service returned an invalid NDWI histogram.')
        counts,edges=histogram; valid=sum(counts)
        if valid<=0: raise ValueError('No valid satellite pixels are available for NDWI in this region and scene.')
        water=0.0
        for i,count in enumerate(counts):
            lo,hi=edges[i:i+2]
            if lo>=threshold: water+=count
            elif hi>threshold and hi>lo: water+=count*(hi-threshold)/(hi-lo)
        fraction=min(1,max(0,water/valid)) if valid else 0; area=round(r['area_km2']*fraction,2); land=round(max(0,r['area_km2']-area),2); cc=body.get('cloudCover',0); obs=body.get('observationDate',date.today().isoformat()); mean=round(stats['mean'],4)
        c=connect(); c.execute('INSERT OR REPLACE INTO water_analyses(region_id,scene_id,observation_date,cloud_cover,ndwi_threshold,total_area_km2,water_area_km2,water_fraction,mean_ndwi,valid_pixels) VALUES(?,?,?,?,?,?,?,?,?,?)',(rid,scene.strip(),obs,cc,threshold,r['area_km2'],area,fraction,mean,round(valid))); c.commit(); c.close()
        return jsonify(analysis=dict(regionId=rid,regionName=r['name'],sceneId=scene.strip(),observationDate=obs,cloudCover=cc,ndwiThreshold=threshold,totalPolygonAreaKm2=r['area_km2'],validPixelCount=round(valid),waterPixelCount=round(water),waterFraction=round(fraction,4),waterAreaKm2=area,landAreaKm2=land,meanNdwi=mean,medianNdwi=round(stats.get('median',mean),4),minNdwi=round(stats['min'],4),maxNdwi=round(stats['max'],4),cached=False))
    except requests.Timeout:
        return jsonify(error='Water-area calculation timed out at the satellite imagery service. Please retry later or choose another scene.'),504
    except requests.HTTPError as e:
        status=e.response.status_code if e.response is not None else None
        if status==504:
            return jsonify(error='The satellite imagery service timed out while calculating NDWI. Please retry later or choose another scene.'),504
        return jsonify(error=f'Water-area calculation failed: imagery service returned HTTP {status or "error"}.'),502
    except (requests.RequestException,KeyError,IndexError,TypeError,ValueError) as e:
        return jsonify(error=f'Water-area calculation failed: {e}'),502

@app.get('/api/analysis/water-history')
def water_history():
    c=connect(); row=c.execute('SELECT * FROM regions WHERE id=?',(request.args.get('regionId','koshi-tappu'),)).fetchone(); c.close()
    if not row: return jsonify(error='Region not found'),404
    try: threshold=float(request.args.get('ndwiThreshold',0)); assert -.5<=threshold<=.5
    except (ValueError,AssertionError): return jsonify(error='ndwiThreshold must be between -0.5 and 0.5.'),400
    r=region(row); key=f'wetland-series:{r["id"]}:{threshold:.2f}'; series=cached(key)
    if series is None:
        windows=[('Apr 2023 (Dry)','2023-03-15','2023-05-15'),('Oct 2023 (Post-Monsoon)','2023-09-25','2023-11-10'),('Apr 2024 (Dry)','2024-03-15','2024-05-15'),('Oct 2024 (Post-Flood)','2024-09-29','2024-11-15'),('Nov 2024 (Winter)','2024-11-16','2024-12-30'),('Apr 2025 (Dry)','2025-03-15','2025-05-15')]
        def seasonal(window):
            label,start,end=window
            try:
                body={'collections':['sentinel-2-l2a'],'bbox':r['bbox'],'datetime':f'{start}T00:00:00Z/{end}T23:59:59Z','query':{'eo:cloud_cover':{'lt':20}},'limit':40,'sortby':[{'field':'properties.datetime','direction':'desc'}]}; sr=requests.post(STAC,json=body,timeout=18); sr.raise_for_status(); features=sr.json().get('features',[])
                features=[f for f in features if f.get('properties',{}).get('datetime') and float(f.get('properties',{}).get('eo:cloud_cover') or 0)<20 and compute_bbox_overlap_ratio(f.get('bbox',[]),r['bbox'])>=.75]
                if not features: return None
                scene=min(features,key=lambda f:float(f.get('properties',{}).get('eo:cloud_cover') or 0)); sid=scene['id']; url=f'{TITILER}/item/statistics?collection=sentinel-2-l2a&item={quote(sid)}&expression=(B03-B08)/(B03%2BB08)&asset_as_band=true&histogram_bins=24'; ar=requests.post(url,json=r['geojson'],timeout=18); ar.raise_for_status(); stats=list(ar.json().get('properties',{}).get('statistics',{}).values())[0]; values=water_area_from_histogram(*stats['histogram'],r['area_km2'],threshold)
                return dict(seasonLabel=label,observationDate=scene['properties']['datetime'][:10],sceneId=sid,cloudCover=round(float(scene['properties'].get('eo:cloud_cover') or 0),2),waterAreaKm2=values['waterAreaKm2'],waterPercent=round(values['waterFraction']*100,1),totalAreaKm2=r['area_km2'])
            except (requests.RequestException,KeyError,IndexError,ValueError): return None
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as pool: series=[point for point in pool.map(seasonal,windows) if point]
        if series: cache(key,series,86400)
    return jsonify(region=feature_region(r),ndwiThreshold=threshold,series=series)

def tile_allowed(z,x,y):
    if not (7<=z<=15 and 0<=x<2**z and 0<=y<2**z): return False
    n=2**z; west=x/n*360-180; east=(x+1)/n*360-180; north=math.degrees(math.atan(math.sinh(math.pi*(1-2*y/n)))); south=math.degrees(math.atan(math.sinh(math.pi*(1-2*(y+1)/n))))
    return east>=85.7 and west<=88.1 and north>=25.7 and south<=27.5
def tile_response(url,path,kind):
    if path.exists(): return send_file(path,mimetype=kind,max_age=604800)
    try:
        r=requests.get(url,timeout=8,headers={'User-Agent':'KoshiWatch/1.0 (Environmental Platform)'}); r.raise_for_status(); path.write_bytes(r.content); return send_file(path,mimetype=r.headers.get('content-type',kind),max_age=604800)
    except requests.RequestException: return ('',502)
@app.get('/api/tiles/jrc/<layer>/<int:z>/<int:x>/<int:y>')
def jrc_tile(layer,z,x,y):
    if layer not in ('occurrence','transitions') or not tile_allowed(z,x,y) or z>13: return ('',404)
    base='https://storage.googleapis.com/global-surface-water/tiles2021'; return tile_response(f'{base}/{layer}/{z}/{x}/{y}.png',CACHE/'jrc'/f'{layer}_{z}_{x}_{y}.png','image/png')
@app.get('/api/tiles/gibs/<product>/<date_text>/<int:z>/<int:x>/<int:y>')
def gibs_tile(product,date_text,z,x,y):
    layers={'true':'MODIS_Terra_CorrectedReflectance_TrueColor','flood':'MODIS_Terra_CorrectedReflectance_Bands721'}
    try: requested=date.fromisoformat(date_text)
    except ValueError: return ('',400)
    if product not in layers or requested<date(2000,2,24) or requested>=date.today() or z<7 or z>9 or not tile_allowed(z,x,y): return ('',404)
    cache_path=CACHE/'gibs'/f'{product}_{date_text}_{z}_{x}_{y}.jpg'
    if cache_path.exists(): return send_file(cache_path,mimetype='image/jpeg',max_age=604800)
    url=f'https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/{layers[product]}/default/{date_text}/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg'
    return tile_response(url,cache_path,'image/jpeg')
@app.get('/api/tiles/imagery/<int:z>/<int:x>/<int:y>')
def imagery_tile(z,x,y):
    if not tile_allowed(z,x,y): return ('',404)
    return tile_response(f'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',CACHE/'imagery'/f'{z}_{x}_{y}.jpg','image/jpeg')
@app.get('/api/tiles/basemap/<path:version>/<int:z>/<int:x>/<int:y>')
def basemap_version_tile(version,z,x,y): return basemap_tile(z,x,y)
@app.get('/api/tiles/basemap/<int:z>/<int:x>/<int:y>')
def basemap_simple_tile(z,x,y): return basemap_tile(z,x,y)
def basemap_tile(z,x,y):
    fallback=bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6364f8cf500f00038605c4e6f15ab70000000049454e44ae426082')
    if z<0 or z>19: return (fallback,200,{'Content-Type':'image/png','Cache-Control':'public,max-age=86400'})
    for key,url,ct in [('esri',f'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}','image/jpeg'),('osm',f'https://tile.openstreetmap.org/{z}/{x}/{y}.png','image/png')]:
        path=CACHE/'basemap'/f'{key}_{z}_{x}_{y}.tile'
        if path.exists(): return send_file(path,mimetype=ct,max_age=86400)
        try:
            r=requests.get(url,headers={'User-Agent':'KoshiWatch/1.0 (Environmental Platform)'},timeout=7); r.raise_for_status()
            if len(r.content)>1000:
                path.write_bytes(r.content); return (r.content,200,{'Content-Type':r.headers.get('content-type',ct),'Cache-Control':'public,max-age=86400'})
        except requests.RequestException: pass
    return (fallback,200,{'Content-Type':'image/png','Cache-Control':'public,max-age=3600'})

@app.get('/api/hydrology/flood')
def flood():
    station=next((s for s in STATIONS if s['id']==request.args.get('stationId')),STATIONS[0])
    requested_year=request.args.get('year','latest')
    if requested_year!='latest':
        try: selected_year=int(requested_year)
        except ValueError: return jsonify(error='year must be latest or a year from 1984 through the current year.'),400
        if not 1984<=selected_year<=date.today().year:
            return jsonify(error=f'year must be latest or between 1984 and {date.today().year}.'),400
    key=f"flood:v2:{station['id']}:{requested_year}"; data=cached(key)
    if data is not None: return jsonify(data)
    try:
        params={'latitude':station['latitude'],'longitude':station['longitude'],'daily':'river_discharge,river_discharge_mean,river_discharge_max','timezone':'GMT'}
        if requested_year=='latest':
            params.update(past_days=92,forecast_days=10)
            source='Open-Meteo GloFAS v4 River Discharge Reanalysis & Forecast'
        else:
            params.update(start_date=f'{selected_year}-01-01',end_date=min(f'{selected_year}-12-31',date.today().isoformat()))
            source='Open-Meteo GloFAS v4 Seamless Daily River Discharge'
        d=fetch_json('https://flood-api.open-meteo.com/v1/flood',params=params); daily=d.get('daily',{}); times=daily.get('time',[]); vals=daily.get('river_discharge',[]); means=daily.get('river_discharge_mean',[]); maxs=daily.get('river_discharge_max',[]); series=[dict(date=t,dischargeM3s=vals[i] if i<len(vals) else None,meanM3s=means[i] if i<len(means) else None,maxM3s=maxs[i] if i<len(maxs) else None) for i,t in enumerate(times)]; observed=[p for p in series if p['dischargeM3s'] is not None and p['date']<=date.today().isoformat()]; latest=round(observed[-1]['dischargeM3s'],1) if observed else None; peak=max(observed,key=lambda p:p['dischargeM3s']) if observed else None; out=dict(station=station,source=source,year=selected_year if requested_year!='latest' else None,period=requested_year,latestDischargeM3s=latest,peakObservedM3s=round(peak['dischargeM3s'],1) if peak else None,peakObservedDate=peak['date'] if peak else None,series=series); cache(key,out,10800); return jsonify(out)
    except requests.RequestException as e: return jsonify(error=str(e)),502

@app.get('/api/hydrology/rainfall')
def rainfall():
    try:
        ya=int(request.args.get('yearA',2024)); yb=int(request.args.get('yearB',2008)); season='full' if request.args.get('season')=='full' else 'monsoon'; year=date.today().year
        if not 1950<=ya<=year or not 1950<=yb<=year: return jsonify(error=f'Years must be between 1950 and {year}.'),400
        start,end=('01-01','12-31') if season=='full' else ('06-01','10-15')
        def rain(y):
            sd=f'{y}-{start}'; ed=min(f'{y}-{end}',date.today().isoformat()); return fetch_json('https://archive-api.open-meteo.com/v1/archive',params={'latitude':26.68,'longitude':87.03,'start_date':sd,'end_date':ed,'daily':'precipitation_sum','timezone':'Asia/Kathmandu'}).get('daily',{})
        a,b=rain(ya),rain(yb); da,db=a.get('time',[]),b.get('time',[]); pa,pb=a.get('precipitation_sum',[]),b.get('precipitation_sum',[])
        def summary(ds,ps):
            pairs=[(d,float(ps[i] or 0)) for i,d in enumerate(ds) if i<len(ps) and ps[i] is not None]; peak=max(pairs,key=lambda x:x[1]) if pairs else ('',0)
            return dict(totalPrecipitationMm=round(sum(v for _,v in pairs),1),peakDailyMm=round(peak[1],1),peakDate=peak[0] or None,wetDaysCount=sum(v>=2.5 for _,v in pairs),heavyMonsoonDaysCount=sum(v>=50 for _,v in pairs))
        series=[]; ca=cb=0
        for i in range(max(len(da),len(db))):
            va=float(pa[i] or 0) if i<len(pa) else 0; vb=float(pb[i] or 0) if i<len(pb) else 0; ca+=va; cb+=vb; dA=da[i] if i<len(da) else ''; dB=db[i] if i<len(db) else ''; series.append(dict(dayLabel=(dA or dB)[5:],dateA=dA,dateB=dB,precipYearA=round(va,1),precipYearB=round(vb,1),cumYearA=round(ca,1),cumYearB=round(cb,1)))
        return jsonify(yearA=ya,yearB=yb,windowLabel='Monsoon Season (Jun 1 – Oct 15)' if season=='monsoon' else 'Full Calendar Year',coordinates=dict(latitude=26.68,longitude=87.03,label='Lower Koshi Catchment (Chatara–Koshi Tappu)'),source='Open-Meteo ERA5 Reanalysis Daily Precipitation',summaryA=summary(da,pa),summaryB=summary(db,pb),series=series)
    except (requests.RequestException,ValueError) as e: return jsonify(error=str(e)),502

@app.get('/api/rivers/satellite-year')
def satellite_year():
    raw=request.args.get('year','latest').lower(); current=date.today().year
    if raw!='latest' and (not raw.isdigit() or not 1988<=int(raw)<=current): return jsonify(error=f'year must be "latest" or an integer between 1988 and {current}.'),400
    requested=current if raw=='latest' else int(raw)
    include_course=request.args.get('includeCourse')!='false'
    key=f'river-sat:v6:{raw}:{int(include_course)}'
    result=cached(key)
    if result is not None:
        # TypeScript caches the observation itself, then wraps it with its optional course per request.
        if 'observation' not in result:
            observation=result; result={'observation':observation,'course':None}
            if include_course:
                try: result['course']=extract_river_course(observation,requested)
                except (requests.RequestException,ValueError,IndexError,KeyError): pass
        return jsonify(result)
    years=[requested] if raw=='latest' else [requested,requested-1,requested+1]
    try:
        obs=None
        for yr in years:
            if yr<1988 or yr>current: continue
            sentinel=raw=='latest' or yr>=2016; collection='sentinel-2-l2a' if sentinel else 'landsat-c2-l2'
            def search_window(start,end,max_cloud):
                body={'collections':[collection],'bbox':[86.88,26.44,87.14,26.86],'datetime':f'{start}T00:00:00Z/{end}T23:59:59Z','query':{'eo:cloud_cover':{'lt':max_cloud}},'limit':40,'sortby':[{'field':'properties.eo:cloud_cover','direction':'asc'}]}
                response=requests.post(STAC,json=body,timeout=15)
                return response.json().get('features',[]) if response.ok else []
            today=date.today()
            if raw=='latest':
                feats=search_window((today-timedelta(days=120)).isoformat(),today.isoformat(),28)
                if not feats: feats=search_window((today-timedelta(days=240)).isoformat(),today.isoformat(),40)
            elif yr==current:
                start=f'{yr}-09-15' if today.month>=9 else f'{yr}-01-01'; feats=search_window(start,today.isoformat(),28)
                if not feats: feats=search_window(f'{yr}-01-01',today.isoformat(),35)
            else:
                feats=search_window(f'{yr}-09-15',f'{yr}-12-31',28)
                has_path140=any('_14004' in f.get('id','') for f in feats)
                if not feats or (not sentinel and not has_path140):
                    full_year=search_window(f'{yr}-01-01',f'{yr}-12-31',28)
                    if full_year: feats=full_year
            if not feats: continue
            def score(f):
                dt=f.get('properties',{}).get('datetime',''); month=int(dt[5:7]) if len(dt)>=7 else 1; pid=f.get('id',''); cloud=float(f.get('properties',{}).get('eo:cloud_cover') or 100); path_bonus=2.0 if not sentinel and '_14004' in pid else 0; row_bonus=.35 if not sentinel and '_140042' in pid else 0; tier_bonus=.8 if not sentinel and pid.endswith('_T1') else 0; season=.6 if 9<=month<=12 else .3 if 1<=month<=5 else 0
                return path_bonus+row_bonus+tier_bonus+1.5*compute_bbox_overlap_ratio(f.get('bbox',[0,0,0,0]),[86.88,26.44,87.14,26.86])+season-cloud/100
            feats.sort(key=score,reverse=True); best=feats[0]; props=best.get('properties',{}); dt=props.get('datetime',''); actual=int(dt[:4]); scene=best['id']; bbox=best['bbox']; ib=[max(bbox[0],86.72),max(bbox[1],26.36),min(bbox[2],87.22),min(bbox[3],26.92)]
            if ib[0]>=ib[2] or ib[1]>=ib[3]: ib=[86.72,26.36,87.22,26.92]
            aspect=(ib[2]-ib[0])*math.cos(math.radians((ib[1]+ib[3])/2))/(ib[3]-ib[1]); w,h=(1152,max(256,round(1152/aspect))) if aspect>=1 else (max(256,round(1152*aspect)),1152)
            base=f'/api/satellite/image?collection={collection}&item={quote(scene,safe="")}&bbox={quote(",".join(map(str,ib)))}&w={w}&h={h}'
            if collection=='sentinel-2-l2a': sensor=f'ESA Sentinel-2 ({scene[:3]}, 10 m)'
            elif scene.startswith('LC09'): sensor='Landsat 9 OLI-2 (30 m)'
            elif scene.startswith('LC08'): sensor='Landsat 8 OLI (30 m)'
            elif scene.startswith('LE07'): sensor='Landsat 7 ETM+ (30 m)'
            elif scene.startswith('LT05'): sensor='Landsat 5 TM (30 m)'
            else: sensor='USGS Landsat C2-L2 (30 m)'
            companion=None
            if collection=='sentinel-2-l2a':
                tile=props.get('s2:mgrs_tile') or (scene.split('_')[3] if len(scene.split('_'))>3 else '')
                companion=next((f for f in feats[1:] if f.get('properties',{}).get('datetime','')[:10]==dt[:10] and (f.get('properties',{}).get('s2:mgrs_tile') or (f.get('id','').split('_')[3] if len(f.get('id','').split('_'))>3 else ''))!=tile),None)
            elif bbox:
                overlap=max(0,min(bbox[2],87.22)-max(bbox[0],86.72))*max(0,min(bbox[3],26.92)-max(bbox[1],26.36))/max(1e-12,(87.22-86.72)*(26.92-26.36))
                if overlap<.98: companion=next((f for f in feats[1:] if f.get('properties',{}).get('datetime','')[:10]==dt[:10]),None)
            obs=dict(requestedYear=requested,actualYear=actual,fallbackUsed=actual!=requested,collection=collection,sensorLabel=sensor,sceneId=scene,observationDate=dt[:10],cloudCover=round(float(props.get('eo:cloud_cover') or 0),2),bbox=bbox,imageBounds=ib,visualImageUrl=base+'&mode=visual',ndwiImageUrl=base+'&mode=ndwi')
            if companion:
                cb=companion.get('bbox'); cprops=companion.get('properties',{}); cbox=[max(cb[0],86.72),max(cb[1],26.36),min(cb[2],87.22),min(cb[3],26.92)]
                if cbox[0]<cbox[2] and cbox[1]<cbox[3]:
                    ca=(cbox[2]-cbox[0])*math.cos(math.radians((cbox[1]+cbox[3])/2))/(cbox[3]-cbox[1]); cw,ch=(1152,max(256,round(1152/ca))) if ca>=1 else (max(256,round(1152*ca)),1152); cbp=quote(','.join(map(str,cbox))); cbase=f'/api/satellite/image?collection={collection}&item={quote(companion["id"],safe="")}&bbox={cbp}&w={cw}&h={ch}'
                    obs.update(companionSceneId=companion['id'],companionVisualImageUrl=cbase+'&mode=visual',companionNdwiImageUrl=cbase+'&mode=ndwi',companionImageBounds=cbox)
            obs={'observation':obs,'course':None}; break
        if obs is None: return jsonify(error=f'No satellite scene found for {requested} or nearby years.'),502
        observation=obs['observation']
        if include_course:
            try: obs['course']=extract_river_course(observation,requested)
            except (requests.RequestException,ValueError,IndexError,KeyError): obs['course']=None
        else: obs['course']=None
        cache(key,obs,21600 if raw=='latest' or requested==current else 2592000)
        return jsonify(obs)
    except requests.RequestException as e: return jsonify(error=f'Satellite scene service unavailable: {e}'),502

def extract_river_course(observation, requested_year):
    """Port of satelliteService.getExtractedRiverCourse using the original TiTiler NDWI matrix."""
    scene=observation['sceneId']; year=observation['actualYear']; key=f"extracted-course:v3:{'latest' if requested_year=='latest' else year}:{scene}"
    found=cached(key)
    if found is not None: return found
    bbox='86.82,26.46,87.19,26.87'; collection=observation['collection']; expr='(B03-B08)/(B03%2BB08)' if collection=='sentinel-2-l2a' else '(green-nir08)/(green%2Bnir08)'
    def get_grid(item):
        url=f'{TITILER}/item/bbox/{bbox}/220x280.npy?collection={collection}&item={quote(item)}&expression={expr}&asset_as_band=true&coord_crs=epsg:4326'
        r=requests.get(url,timeout=25); r.raise_for_status(); return r.content
    primary=get_grid(scene); companion=None
    if observation.get('companionSceneId'):
        try: companion=get_grid(observation['companionSceneId'])
        except requests.RequestException: pass
    extracted=extract_channel_lines(primary,companion); main=extracted['mainstem']; metrics=channel_metrics(main,main)
    color=next((color for minimum,color in [(2024,'#0284C7'),(2020,'#0D9488'),(2015,'#7C3AED'),(2008,'#DC2626'),(2000,'#EA580C'),(1988,'#B45309')] if year>=minimum),'#0284C7')
    label=f"{year} Latest ({observation['observationDate']})" if requested_year=='latest' else f"{year} Course ({observation['observationDate']})"
    main['properties']={'id':f'course-{year}','year':year,'label':label,'color':color}
    braids=extracted['braids']
    for i,braid in enumerate(braids): braid['properties']={**(braid.get('properties') or {}),'year':year,'color':color,'braidIndex':i}
    course=dict(id=f'course-{year}',requestedYear=observation['requestedYear'],actualYear=year,observationDate=observation['observationDate'],sensorLabel=observation['sensorLabel'],sceneId=scene,color=color,label=label,mainstem=main,braids=braids,lengthKm=metrics['channelLengthKm'],sinuosity=metrics['sinuosityIndex'],meanChannelLng=extracted['meanChannelLng'],maxShiftKm=0,waterPixelCount=extracted['waterPixelCount'])
    cache(key,course,21600 if requested_year=='latest' else 2592000); return course

@app.get('/')
def index(): return send_from_directory(ROOT/'dist','index.html')
@app.get('/<path:path>')
def static_files(path):
    # public/ is mirrored from dist/ for Vercel's static CDN; serving dist/
    # here preserves the existing Flask development workflow.
    target=ROOT/'dist'/path
    if target.is_file(): return send_from_directory(ROOT/'dist',path)
    return send_from_directory(ROOT/'dist','index.html')

if __name__=='__main__':
    connect().close()
    app.run(host='127.0.0.1',port=int(os.environ.get('PORT','5000')),debug=False)
