from __future__ import annotations
import json, math
from pathlib import Path
import nuke
from .surface_guides import source_frame_range

def pixel_aspect(node):
    try:
        v=float(node.format().pixelAspect()) if node is not None else 1.0
        return v if math.isfinite(v) and v>0 else 1.0
    except Exception:
        return 1.0

def source_format_info(node):
    w=int(node.width()) if node is not None else 0
    h=int(node.height()) if node is not None else 0
    pa=pixel_aspect(node)
    return {"width":w,"height":h,"pixel_aspect":pa,"display_width":float(w)*pa,"display_height":float(h)}

def square_pixel_proxy_dimensions(width,height,pa,max_dimension):
    dw=float(width)*float(pa); dh=float(height)
    s=min(1.0,float(max_dimension)/max(dw,dh,1.0))
    return max(2,int(round(dw*s))), max(2,int(round(dh*s)))

def ensure_square_pixel_proxy(controller,source_read,output_folder,max_dimension,log=None):
    sf=source_format_info(source_read); pa=float(sf["pixel_aspect"])
    if abs(pa-1.0)<1e-6:
        return {"images_dir":None,"source_format":sf,"created":False}
    fr=source_frame_range(source_read)
    if fr is None: raise RuntimeError("AutoLens could not determine the source Read frame range.")
    first,last=fr
    tw,th=square_pixel_proxy_dimensions(sf["width"],sf["height"],pa,int(max_dimension))
    out=Path(output_folder).resolve(); cache_root=out.parent
    proxy=cache_root/f"_squarepixel_proxy_{tw}x{th}_pa{f'{pa:.6g}'.replace('.', 'p')}"
    proxy.mkdir(parents=True,exist_ok=True)
    manifest_path=proxy/'autolens_proxy_manifest.json'
    try: source_pattern=nuke.filename(source_read) or ""
    except Exception: source_pattern=""
    manifest={"version":1,"source_pattern":source_pattern,"first":int(first),"last":int(last),"source_width":int(sf["width"]),"source_height":int(sf["height"]),"source_pixel_aspect":pa,"proxy_width":tw,"proxy_height":th}
    existing=None
    if manifest_path.exists():
        try: existing=json.loads(manifest_path.read_text(encoding='utf-8'))
        except Exception: pass
    ff=proxy/f"frame.{first:06d}.png"; lf=proxy/f"frame.{last:06d}.png"
    if existing==manifest and ff.exists() and lf.exists():
        if log: log(f"\n[AUTOLENS PAR] Reusing cached square-pixel solve proxy:\n  {proxy}\n")
        return {"images_dir":str(proxy),"source_format":sf,"created":False}
    input_node=None
    if controller is not None:
        try: input_node=controller.input(0)
        except Exception: pass
    input_node=input_node or source_read
    if log: log(f"\n[AUTOLENS PAR] Non-square source pixels detected.\n  Source: {sf['width']} x {sf['height']}  PAR {pa:g}\n  Solve proxy: {tw} x {th}  PAR 1.0\n  Rendering {first}-{last}; proxy will be cached.\n")
    rf=wr=None
    try:
        rf=nuke.nodes.Reformat(); rf.setInput(0,input_node)
        for k,v in {"type":"to box","box_width":tw,"box_height":th,"box_pixel_aspect":1.0,"box_fixed":True,"resize":"distort","center":True,"pbb":False}.items():
            if rf.knob(k): rf[k].setValue(v)
        if rf.knob('filter'):
            try: rf['filter'].setValue('Cubic')
            except Exception: pass
        wr=nuke.nodes.Write(); wr.setInput(0,rf); wr['file'].setValue(str(proxy/'frame.%06d.png').replace('\\','/'))
        if wr.knob('file_type'): wr['file_type'].setValue('png')
        if wr.knob('channels'): wr['channels'].setValue('rgb')
        nuke.execute(wr.name(),int(first),int(last))
    finally:
        if wr is not None:
            try: nuke.delete(wr)
            except Exception: pass
        if rf is not None:
            try: nuke.delete(rf)
            except Exception: pass
    if not ff.exists() or not lf.exists(): raise RuntimeError("AutoLens did not create the expected square-pixel proxy frames.")
    manifest_path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return {"images_dir":str(proxy),"source_format":sf,"created":True}
