import type {PlanetExpression} from './sphere-avatar-protocol';
export type Identity = 'Earth' | 'Mars' | 'Saturn';

function capsule(cx:number,cy:number,rx:number,ry:number,bend=0){return `M ${cx-rx} ${cy} C ${cx-rx} ${cy-ry*.8+bend} ${cx-rx*.6} ${cy-ry+bend} ${cx} ${cy-ry+bend} C ${cx+rx*.7} ${cy-ry+bend} ${cx+rx} ${cy-ry*.75+bend} ${cx+rx} ${cy} C ${cx+rx} ${cy+ry*.85+bend} ${cx+rx*.65} ${cy+ry+bend} ${cx} ${cy+ry+bend} C ${cx-rx*.7} ${cy+ry+bend} ${cx-rx} ${cy+ry*.75+bend} ${cx-rx} ${cy}Z`;}
export function pose(expression:PlanetExpression){
 let left={x:109,y:176,w:15,h:34,b:0},right={x:185,y:173,w:16,h:33,b:0};let brows=0,tilt=0,tear=0;
 switch(expression){
  case 'attentive':left.h=39;right.h=38;left.w=16;right.w=17;break;
  case 'curious':left.h=39;right.h=27;right.y=164;left.y=174;brows=.65;break;
  case 'focused':left.h=21;right.h=17;left.y=179;right.y=174;left.b=4;right.b=-4;brows=.4;break;
  case 'thinking':left.x=104;right.x=180;left.h=30;right.h=25;right.y=167;brows=.8;tilt=-4;break;
  case 'talking':left.h=28;right.h=37;left.y=174;right.y=177;left.w=17;break;
  case 'waiting':left.h=14;right.h=13;left.y=179;right.y=176;left.b=3;right.b=3;break;
  case 'happy':left.h=6;right.h=6;left.w=16;right.w=17;left.b=-12;right.b=-12;break;
  case 'proud':left.h=14;right.h=9;left.y=173;right.y=169;left.b=-4;right.b=-6;tilt=-3;break;
  case 'surprised':left.w=19;left.h=41;right.w=19;right.h=41;brows=.8;break;
  case 'sleepy':left.h=4.1;right.h=4.1;left.b=6;right.b=6;left.y=181;right.y=179;break;
  case 'concerned':left.h=24;right.h=23;left.y=182;right.y=178;left.b=5;right.b=-5;brows=.8;break;
  case 'sad':left.y=186;right.y=183;left.h=15;right.h=15;left.b=8;right.b=8;tear=.7;break;
  case 'calm':left.h=4;right.h=4;left.b=2;right.b=2;break;
  case 'relieved':left.h=4.5;right.h=4.5;left.b=9;right.b=9;left.w=16;right.w=17;break;
  case 'wink':right.h=2;right.b=-5;left.h=31;right.w=18;break;
 }
 left.x+=10;right.x+=11;left.y-=25;right.y-=25;
 return {left:capsule(left.x,left.y,left.w,left.h,left.b),right:capsule(right.x,right.y,right.w,right.h,right.b),brows,tilt,tear};
}
