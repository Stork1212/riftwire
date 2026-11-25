from PIL import Image,ImageDraw,ImageFont
from pathlib import Path
import math, subprocess
P=Path(__file__).parent
BG='#101014'; FG='#F8F8F8'; AC='#FF5635'
FONT='/usr/share/fonts/truetype/lato/Lato-HeavyItalic.ttf'
def mark(d,x,y,s):
 def poly(points,c):d.polygon([(x+a*s,y+b*s) for a,b in points],fill=c)
 poly([(0,0),(65,0),(88,23),(88,48),(65,66),(94,100),(64,100),(25,56),(25,100),(0,100)],FG)
 poly([(25,19),(56,19),(65,28),(65,38),(56,44),(25,44)],BG)
 poly([(79,0),(115,0),(96,20)],AC)
def text(d,xy,t,size,color=FG):d.text(xy,t,font=ImageFont.truetype(FONT,size),fill=color,anchor='mm')
im=Image.new('RGB',(800,800),BG);mark(ImageDraw.Draw(im),228,250,3);im.save(P/'profile.png')
im=Image.new('RGB',(2560,1440),BG);d=ImageDraw.Draw(im)
for i in range(-1000,3500,160):d.line((i,0,i-700,1440),fill='#1B1B22',width=2)
mark(d,570,640,1.5);text(d,(1400,700),'RUMBLEFRAME',125);text(d,(1400,790),'GAMING  /  NEWS  /  HIGHLIGHTS',34,AC);im.save(P/'banner.png')
svg='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 115 100"><path fill="#F8F8F8" fill-rule="evenodd" d="M0 0H65L88 23V48L65 66L94 100H64L25 56V100H0ZM25 19V44H56L65 38V28L56 19Z"/><path fill="#FF5635" d="M79 0H115L96 20Z"/></svg>''';(P/'mark.svg').write_text(svg)
cmd=['ffmpeg','-y','-loglevel','error','-f','rawvideo','-pix_fmt','rgb24','-s','1080x1920','-r','30','-i','-','-c:v','libx264','-pix_fmt','yuv420p','-crf','18','-movflags','+faststart',str(P/'outro.mp4')]
p=subprocess.Popen(cmd,stdin=subprocess.PIPE)
for i in range(75):
 im=Image.new('RGB',(1080,1920),BG);d=ImageDraw.Draw(im);t=i/30
 for j in range(-800,2000,150):d.line((j+int(t*90),0,j-850+int(t*90),1920),fill='#202027',width=2)
 a=min(1,t/.45);a=1-(1-a)**3
 mark(d, int(540-172.5),int(690+80*(1-a)),3)
 text(d,(540,1080),'RUMBLEFRAME',108);text(d,(540,1180),'FOLLOW FOR MORE',45,AC)
 d.rectangle((int(540-300*a),1250,int(540+300*a),1257),fill=AC)
 p.stdin.write(im.tobytes())
p.stdin.close();assert p.wait()==0
