import { afterEach, expect, it, vi } from 'vitest';
import { animateWindowArrival } from './window-arrival';

const originalAnimate=Object.getOwnPropertyDescriptor(Element.prototype,'animate');
afterEach(() => {
  document.body.replaceChildren();vi.restoreAllMocks();
  if(originalAnimate) Object.defineProperty(Element.prototype,'animate',originalAnimate);
  else Reflect.deleteProperty(Element.prototype,'animate');
});

it('keeps an immediate control release through arrival cancellation even when its owner stops bubbling', () => {
  const surface=document.createElement('div');
  const controls=document.createElement('div');const button=document.createElement('button');
  controls.append(button);surface.append(controls);document.body.append(surface);
  const order: string[]=[];
  Object.defineProperty(Element.prototype,'animate',{configurable:true,value:vi.fn(() => ({
    cancel: () => order.push('cancel'), finished: new Promise<Animation>(() => {}),
  }) as unknown as Animation)});
  Object.defineProperty(button,'setPointerCapture',{configurable:true,value:vi.fn(() => order.push('capture'))});
  controls.addEventListener('pointerdown', event => event.stopPropagation());
  const finish=animateWindowArrival(surface);
  const pointer=new Event('pointerdown',{bubbles:true});Object.defineProperty(pointer,'pointerId',{value:7});
  button.dispatchEvent(pointer);
  expect(button.setPointerCapture).toHaveBeenCalledWith(7);
  expect(order).toEqual(['capture','cancel','cancel']);
  expect(surface.querySelector('.paw-window-arrival-dust')).toBeNull();
  finish?.();expect(order).toHaveLength(3);
});
