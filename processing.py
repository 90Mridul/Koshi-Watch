"""Python ports of the original water-index and river-channel processing helpers."""
from __future__ import annotations

import io
import math
from collections import deque

import numpy as np

CHANNEL_BBOX = (86.82, 26.46, 87.19, 26.87)

def compute_bbox_overlap_ratio(scene_bbox, region_bbox):
    sx1,sy1,sx2,sy2=scene_bbox; rx1,ry1,rx2,ry2=region_bbox
    width=max(0,min(sx2,rx2)-max(sx1,rx1)); height=max(0,min(sy2,ry2)-max(sy1,ry1)); area=(rx2-rx1)*(ry2-ry1)
    return min(1.0,width*height/area) if area>0 else 0.0

def compute_polygon_area_km2(feature):
    # Turf area uses the same WGS84 mean Earth radius and spherical ring integral.
    polygons=feature['geometry']['coordinates']; kind=feature['geometry']['type']; polygons=[polygons] if kind=='Polygon' else polygons; total=0.0
    for polygon in polygons:
        for ring in polygon:
            integral=0.0
            for current,nxt in zip(ring,ring[1:]):
                lon1,lat1=map(math.radians,current[:2]); lon2,lat2=map(math.radians,nxt[:2]); integral+=(lon2-lon1)*(2+math.sin(lat1)+math.sin(lat2))
            total+=integral*6371008.8**2/2
    return round(abs(total)/1_000_000,4)

def water_area_from_histogram(counts, edges, area_km2, threshold=0.0):
    if not counts or len(edges) != len(counts) + 1:
        raise ValueError('Invalid NDWI histogram: expected one more edge than bins.')
    if area_km2 <= 0:
        raise ValueError('Total polygon area must be positive.')
    valid = sum(float(v or 0) for v in counts); water = 0.0
    for i, value in enumerate(counts):
        count=float(value or 0); low=float(edges[i]); high=float(edges[i+1])
        if count <= 0: continue
        if low >= threshold: water += count
        elif high > threshold and high > low: water += count*(high-threshold)/(high-low)
    fraction=min(1.0,max(0.0,water/valid)) if valid>0 else 0.0
    water_area=round(area_km2*fraction+1e-12,2)
    return {'validPixelCount':round(valid),'waterPixelCount':round(water),'waterFraction':round(fraction,4),'waterAreaKm2':water_area,'landAreaKm2':round(max(0,area_km2-water_area),2)}

def parse_npy_grid(content: bytes):
    try: array=np.load(io.BytesIO(content),allow_pickle=False)
    except Exception as exc: raise ValueError('Invalid NPY raster received from TiTiler.') from exc
    if array.ndim!=3 or array.shape[0]<2: raise ValueError(f'Unexpected NDWI matrix shape: {array.shape}')
    return np.asarray(array,dtype=np.float64)

def _runs(row):
    result=[]; c=0; w=len(row)
    while c<w:
        if row[c]:
            start=c
            while c+1<w and row[c+1]: c+=1
            end=c; result.append({'start':start,'end':end,'center':(start+end)/2,'width':end-start+1})
        c+=1
    return result

def extract_channel_lines(primary, companion=None, bbox=CHANNEL_BBOX, threshold=0.0):
    """Trace the widest coherent NDWI channel from north to south in real NPY pixels."""
    p=parse_npy_grid(primary) if isinstance(primary,(bytes,bytearray)) else np.asarray(primary,dtype=np.float64)
    q=(parse_npy_grid(companion) if isinstance(companion,(bytes,bytearray)) else np.asarray(companion,dtype=np.float64)) if companion is not None else None
    h,w=p.shape[1:]; pvalid=np.isfinite(p[0]) & (p[1]>0); water=pvalid & (p[0]>threshold)
    if q is not None:
        qvalid=np.isfinite(q[0]) & (q[1]>0); water |= qvalid & (q[0]>threshold); valid=pvalid|qvalid
    else: valid=pvalid
    if int(water.sum())<150:
        water=pvalid & (p[0]>-.03)
        if q is not None: water |= qvalid & (q[0]>-.03)
    # Bridge only invalid scan-line gaps, matching the original mask-aware behavior.
    bridged=water.copy()
    for r in range(1,h-1):
        for c in range(1,w-1):
            if not valid[r,c] and (water[r-1,c] and water[r+1,c] or water[r-1,c-1] and water[r+1,c+1] or water[r-1,c+1] and water[r+1,c-1]): bridged[r,c]=True
    # Keep substantial 8-connected river components, rejecting isolated ponds/specks.
    seen=np.zeros((h,w),dtype=bool); kept=np.zeros((h,w),dtype=bool)
    for r0,c0 in zip(*np.nonzero(bridged & ~seen)):
        if seen[r0,c0]: continue
        todo=[(int(r0),int(c0))]; seen[r0,c0]=True; component=[]; minr=maxr=r0
        while todo:
            r,c=todo.pop(); component.append((r,c)); minr=min(minr,r); maxr=max(maxr,r)
            for dr in (-1,0,1):
                for dc in (-1,0,1):
                    nr,nc=r+dr,c+dc
                    if (dr or dc) and 0<=nr<h and 0<=nc<w and bridged[nr,nc] and not seen[nr,nc]: seen[nr,nc]=True; todo.append((nr,nc))
        if maxr-minr+1>=16 or len(component)>=45:
            rr,cc=zip(*component); kept[list(rr),list(cc)]=True
    rowruns=[_runs(kept[r]) for r in range(h)]; track=[]; prev=None; last=-1
    for r,runs in enumerate(rowruns):
        if not runs: continue
        if prev is None: chosen=max(runs,key=lambda x:x['width'])
        else:
            gap=max(1,r-last); maxjump=min(45,10+gap*2.5); candidates=[x for x in runs if abs(x['center']-prev)<=maxjump]
            chosen=max(candidates,key=lambda x:x['width']*2-abs(x['center']-prev)) if candidates else (max(runs,key=lambda x:x['width']) if gap>12 else None)
        if chosen: track.append({'r':r,'c':chosen['center']}); prev=chosen['center']; last=r
    smooth=[]
    for i,pt in enumerate(track):
        if i==0 or i==len(track)-1: smooth.append(pt); continue
        before,after=track[i-1],track[i+1]
        smooth.append(pt if abs(after['r']-before['r'])>6 or abs(after['c']-before['c'])>15 else {'r':pt['r'],'c':.25*before['c']+.5*pt['c']+.25*after['c']})
    def lng(c): return round(bbox[0]+((c+.5)/w)*(bbox[2]-bbox[0]),5)
    def lat(r): return round(bbox[3]-((r+.5)/h)*(bbox[3]-bbox[1]),5)
    points=[{'r':p['r'],'c':p['c']} for i,p in enumerate(smooth) if i==0 or i==len(smooth)-1 or i%2==0]
    coords=[[lng(p['c']),lat(p['r'])] for p in points]
    if len(coords)<2: raise ValueError('Too few unmasked water pixels in scene to extract channel geometry.')
    meanlng=round(sum(x for x,_ in coords)/len(coords),4)
    main={'type':'Feature','geometry':{'type':'LineString','coordinates':coords},'properties':{'kind':'mainstem'}}
    # Secondary row runs are included only when they persist and remain separate from the main trace.
    byrow={p['r']:p['c'] for p in smooth}; active=[]; finished=[]
    for r in range(h):
        sec=[x for x in rowruns[r] if r not in byrow or abs(x['center']-byrow[r])>=4]; used=set()
        for bi in range(len(active)-1,-1,-1):
            branch=active[bi]; old=branch[-1]; matches=[(abs(x['center']-old['c']),i,x) for i,x in enumerate(sec) if i not in used and abs(x['center']-old['c'])<7]
            if matches:
                _,i,x=min(matches); used.add(i); branch.append({'r':r,'c':x['center']})
            elif r-old['r']>3:
                if len(branch)>=12: finished.append(branch)
                active.pop(bi)
        for i,x in enumerate(sec):
            if i not in used: active.append([{'r':r,'c':x['center']}])
    finished.extend(b for b in active if len(b)>=12)
    braids=[]
    for i,branch in enumerate(finished[:8]):
        bc=[[lng(p['c']),lat(p['r'])] for j,p in enumerate(branch) if j==0 or j==len(branch)-1 or j%2==0]
        braids.append({'type':'Feature','geometry':{'type':'LineString','coordinates':bc},'properties':{'braidIndex':i,'kind':'braid'}})
    return {'mainstem':main,'braids':braids,'waterPixelCount':int(water.sum()),'meanChannelLng':meanlng}

def haversine(a,b):
    r=6371; p1,p2=math.radians(a[1]),math.radians(b[1]); dp=p2-p1; dl=math.radians(b[0]-a[0]); q=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return r*2*math.atan2(math.sqrt(q),math.sqrt(1-q))

def channel_metrics(feature, reference=None):
    coords=feature['geometry']['coordinates']
    if len(coords)<2: raise ValueError('Channel LineString must have at least 2 coordinate pairs.')
    length=sum(haversine(a,b) for a,b in zip(coords,coords[1:])); chord=haversine(coords[0],coords[-1]); shifts=[]
    if reference:
        ref=reference['geometry']['coordinates']; shifts=[min(haversine(p,q) for q in ref) for p in coords]
    return {'channelLengthKm':round(length,2),'chordLengthKm':round(chord,2),'sinuosityIndex':round(length/chord,3) if chord else 1,'meanLateralShiftKm':round(sum(shifts)/len(shifts),2) if shifts else 0,'maxLateralShiftKm':round(max(shifts),2) if shifts else 0}

def rainfall_summary(dates, values):
    pairs=[(d,float(values[i] or 0)) for i,d in enumerate(dates) if i<len(values) and values[i] is not None and math.isfinite(float(values[i]))]; peak=max(pairs,key=lambda x:x[1]) if pairs else ('',0)
    return {'totalPrecipitationMm':round(sum(v for _,v in pairs),1),'peakDailyMm':round(peak[1],1),'peakDate':peak[0] or None,'wetDaysCount':sum(v>=2.5 for _,v in pairs),'heavyMonsoonDaysCount':sum(v>=50 for _,v in pairs)}
