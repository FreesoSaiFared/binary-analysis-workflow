#!/usr/bin/env python3
import json

def conv_macs(h,w,cin,cout,k=3):
    return h*w*cin*cout*k*k

def budget(w,h,c=16,blocks=4,inc=7):
    macs=conv_macs(h,w,inc,c)
    macs += blocks*2*conv_macs(h,w,c,c)
    macs += conv_macs(h,w,c,3)
    return {"width":w,"height":h,"GMAC":macs/1e9,"GOP_2ops_per_MAC":2*macs/1e9}

if __name__=="__main__":
    print(json.dumps([budget(640,360),budget(960,540),budget(1280,720)],indent=2))
