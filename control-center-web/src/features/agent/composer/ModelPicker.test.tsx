import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ModelCatalog, ThinkingLevel } from '../types';
import { ModelPicker } from './ModelPicker';
import { ChatPresentationProvider } from '@/features/conversation-ui/reading/chat-presentation';

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    clearRect:vi.fn(), beginPath:vi.fn(), arc:vi.fn(), fill:vi.fn(),
  } as unknown as CanvasRenderingContext2D);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

function catalog(): ModelCatalog {
  return {
    schemaVersion: 'rag-ime.agent-model-catalog.v1',
    ok: true,
    thinkingLevel: 'high',
    selected: {
      provider: 'gpt',
      id: 'gpt-5.6-luna',
      modelId: 'gpt-5.6-luna',
      name: 'GPT-5.6 Luna',
    },
    providers: [
      {
        id: 'gpt',
        displayName: 'OpenAI Codex',
        models: [
          {
            provider: 'gpt',
            id: 'gpt-5.6-luna',
            name: 'GPT-5.6 Luna',
            api: 'responses',
            reasoning: true,
            thinkingLevels: ['off', 'medium', 'high', 'max'],
            supportsImages: true,
            contextWindow: 272_000,
            maxTokens: 64_000,
          },
        ],
      },
      {
        id: 'deepseek',
        displayName: 'DeepSeek',
        models: [
          {
            provider: 'deepseek',
            id: 'deepseek-v4-flash',
            name: 'DeepSeek V4 Flash',
            api: 'chat-completions',
            reasoning: true,
            thinkingLevels: ['off', 'low'],
            supportsImages: false,
            contextWindow: 128_000,
            maxTokens: 32_000,
          },
        ],
      },
    ],
  } as unknown as ModelCatalog;
}

function renderPicker(onChange = vi.fn()) {
  render(
    <ModelPicker
      catalog={catalog()}
      disabled={false}
      pending={false}
      requestOpen={0}
      onChange={onChange}
    />,
  );
  return onChange;
}

describe('merged model and reasoning control', () => {
  it('searches models in the merged popover and preserves a legal level when switching models', async () => {
    const onChange = renderPicker();
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', {
      name: '模型与推理：GPT-5.6 Luna · OpenAI Codex · 高',
    });

    await user.click(trigger);
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    expect(within(picker).getByRole('radiogroup', { name: '推理强度' })).toBeInTheDocument();
    expect(within(picker).queryByRole('listbox')).not.toBeInTheDocument();
    await user.click(within(picker).getByRole('button', { name: /更换模型/ }));
    const search = within(picker).getByRole('searchbox', { name: '搜索模型' });
    await waitFor(() => expect(search).toHaveFocus());
    await user.type(search, 'flash');
    expect(within(picker).queryByRole('option', { name: '选择模型 GPT-5.6 Luna' }))
      .not.toBeInTheDocument();
    const flash = within(picker).getByRole('option', { name: '选择模型 DeepSeek V4 Flash' });
    await user.keyboard('{ArrowDown}');
    expect(flash).toHaveFocus();
    await user.keyboard('{Enter}');

    expect(onChange).toHaveBeenCalledWith('deepseek', 'deepseek-v4-flash', 'off');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '选择模型与推理强度' }))
      .not.toBeInTheDocument());
  });

  it('changes only the current model reasoning level and returns focus on Escape', async () => {
    const onChange = renderPicker();
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', {
      name: '模型与推理：GPT-5.6 Luna · OpenAI Codex · 高',
    });

    await user.click(trigger);
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    await waitFor(() => expect(within(picker).getByRole('radio', { name: '高' })).toHaveFocus());
    await user.keyboard('{ArrowRight}{Enter}');
    expect(onChange).toHaveBeenCalledWith('gpt', 'gpt-5.6-luna', 'max');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '选择模型与推理强度' }))
      .not.toBeInTheDocument());

    await user.click(trigger);
    expect(screen.getByRole('dialog', { name: '选择模型与推理强度' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '选择模型与推理强度' }))
      .not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it('exposes one merged control whose accessible name carries both facts', () => {
    renderPicker();
    const trigger = screen.getByRole('button', {
      name: '模型与推理：GPT-5.6 Luna · OpenAI Codex · 高',
    });
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(trigger).toHaveClass('agent-composer__picker');
    expect(trigger).toHaveTextContent('GPT-5.6 Luna');
    expect(trigger).toHaveTextContent('高');
  });

  it('opens the merged popover on the requested section for each picker command', async () => {
    const { rerender } = render(
      <ModelPicker
        catalog={catalog()}
        disabled={false}
        pending={false}
        requestOpen={0}
        thinkingRequestOpen={0}
        onChange={vi.fn()}
      />,
    );

    rerender(
      <ModelPicker
        catalog={catalog()}
        disabled={false}
        pending={false}
        requestOpen={0}
        thinkingRequestOpen={1}
        onChange={vi.fn()}
      />,
    );

    let picker = await screen.findByRole('dialog', { name: '选择模型与推理强度' });
    await waitFor(() => expect(within(picker).getByRole('radio', { name: '高' }))
      .toHaveFocus());
    expect(within(picker).queryByRole('searchbox', { name: '搜索模型' })).not.toBeInTheDocument();

    await userEvent.setup().keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '选择模型与推理强度' }))
      .not.toBeInTheDocument());

    rerender(
      <ModelPicker
        catalog={catalog()}
        disabled={false}
        pending={false}
        requestOpen={1}
        thinkingRequestOpen={1}
        onChange={vi.fn()}
      />,
    );

    picker = await screen.findByRole('dialog', { name: '选择模型与推理强度' });
    await waitFor(() => expect(within(picker).getByRole('searchbox', { name: '搜索模型' }))
      .toHaveFocus());
  });
});

function renderV2(onChange = vi.fn()) {
  const props = {catalog:catalog(), disabled:false, pending:false, requestOpen:0, onChange};
  const view = render(<ChatPresentationProvider ownerKey="test:reasoning-slider-v2" defaultVersion="v2"><ModelPicker {...props}/></ChatPresentationProvider>);
  return {onChange, view, props};
}
function pointer(slider:HTMLElement, type:string) {
  const event = new MouseEvent(type, {bubbles:true, button:0});
  Object.defineProperty(event, 'pointerId', {value:7});
  fireEvent(slider, event);
}
async function openV2() {
  const trigger = screen.getByRole('button', {name:'模型与推理：GPT-5.6 Luna · OpenAI Codex · 高'});
  await userEvent.setup().click(trigger);
  const slider = screen.getByRole('slider', {name:'推理强度'});
  await waitFor(() => expect(slider).toHaveFocus());
  return {trigger,slider};
}
describe('v2 transactional reasoning slider', () => {
  it('previews actual catalog levels during drag and commits once only on release', async () => {
    const {onChange}=renderV2();const {slider}=await openV2();
    expect(slider).toHaveAttribute('max','3');expect(slider).toHaveValue('2');
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'3'}});
    expect(slider).toHaveAttribute('aria-valuetext','最高');
    expect(screen.getByRole('dialog')).toHaveTextContent('最高');expect(onChange).not.toHaveBeenCalled();
    pointer(slider,'pointerup');pointer(slider,'pointerup');
    expect(onChange).toHaveBeenCalledTimes(1);expect(onChange).toHaveBeenCalledWith('gpt','gpt-5.6-luna','max');
  });
  it('keeps v2 reasoning open through pending, shows only owner ACK, and permits retry after a failed selection', async () => {
    const {onChange,view,props}=renderV2();const {slider}=await openV2();
    const update=(pending:boolean,level:ThinkingLevel)=>view.rerender(<ChatPresentationProvider ownerKey="test:reasoning-slider-v2" defaultVersion="v2"><ModelPicker {...props} pending={pending} catalog={{...props.catalog,thinkingLevel:level}}/></ChatPresentationProvider>);
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'3'}});pointer(slider,'pointerup');
    expect(onChange).toHaveBeenCalledTimes(1);expect(screen.getByRole('dialog')).toBeInTheDocument();
    update(true,'high');expect(slider).toBeDisabled();expect(slider).toHaveValue('2');
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'0'}});pointer(slider,'pointerup');
    expect(onChange).toHaveBeenCalledTimes(1);expect(screen.getByRole('dialog')).toHaveTextContent('高');
    vi.useFakeTimers();update(false,'max');expect(slider).toBeEnabled();expect(slider).toHaveValue('3');
    act(()=>vi.advanceTimersByTime(220));expect(document.querySelector('.agent-reasoning-effects')).toHaveAttribute('data-bursts','1');
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'0'}});pointer(slider,'pointerup');
    expect(onChange).toHaveBeenCalledTimes(2);update(true,'max');update(false,'max');
    expect(slider).toHaveValue('3');expect(screen.getByRole('dialog')).toBeInTheDocument();
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'0'}});pointer(slider,'pointerup');
    expect(onChange).toHaveBeenCalledTimes(3);expect(onChange).toHaveBeenLastCalledWith('gpt','gpt-5.6-luna','off');
  });
  it('confirms max once when the native catalog event precedes request settlement', async () => {
    const {view,props}=renderV2();const {slider}=await openV2();
    const update=(pending:boolean,level:ThinkingLevel)=>view.rerender(<ChatPresentationProvider ownerKey="test:reasoning-slider-v2" defaultVersion="v2"><ModelPicker {...props} pending={pending} catalog={{...props.catalog,thinkingLevel:level}}/></ChatPresentationProvider>);
    vi.useFakeTimers();
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'3'}});pointer(slider,'pointerup');
    update(true,'high');update(true,'max');
    act(()=>vi.advanceTimersByTime(400));
    expect(document.querySelector('.agent-reasoning-effects')).toHaveAttribute('data-bursts','0');
    update(false,'max');act(()=>vi.advanceTimersByTime(220));
    expect(document.querySelector('.agent-reasoning-effects')).toHaveAttribute('data-bursts','1');
    update(false,'max');act(()=>vi.advanceTimersByTime(500));
    expect(document.querySelector('.agent-reasoning-effects')).toHaveAttribute('data-bursts','1');
  });
  it('keeps one burst when a held max preview is subsequently confirmed', async () => {
    const {view,props}=renderV2();const {slider}=await openV2();
    const update=(pending:boolean,level:ThinkingLevel)=>view.rerender(<ChatPresentationProvider ownerKey="test:reasoning-slider-v2" defaultVersion="v2"><ModelPicker {...props} pending={pending} catalog={{...props.catalog,thinkingLevel:level}}/></ChatPresentationProvider>);
    vi.useFakeTimers();
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'3'}});
    act(()=>vi.advanceTimersByTime(220));
    expect(document.querySelector('.agent-reasoning-effects')).toHaveAttribute('data-bursts','1');
    pointer(slider,'pointerup');update(true,'high');update(true,'max');update(false,'max');
    act(()=>vi.advanceTimersByTime(500));
    expect(document.querySelector('.agent-reasoning-effects')).toHaveAttribute('data-bursts','1');
  });
  it('discards pointer cancellation and Escape without changing Pi selection', async () => {
    const {onChange}=renderV2();const {slider,trigger}=await openV2();
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'0'}});pointer(slider,'pointercancel');
    expect(slider).toHaveValue('2');expect(slider).toHaveAttribute('aria-valuetext','高');expect(onChange).not.toHaveBeenCalled();
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'3'}});
    await userEvent.setup().keyboard('{Escape}');pointer(slider,'pointerup');
    expect(onChange).not.toHaveBeenCalled();expect(trigger).toHaveFocus();expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it('uses native keyboard preview/release including Home and End', async () => {
    const {onChange}=renderV2();let {slider}=await openV2();
    fireEvent.keyDown(slider,{key:'Home'});fireEvent.change(slider,{target:{value:'0'}});
    expect(onChange).not.toHaveBeenCalled();fireEvent.keyUp(slider,{key:'Home'});
    expect(onChange).toHaveBeenLastCalledWith('gpt','gpt-5.6-luna','off');
    await userEvent.setup().keyboard('{Escape}');
    ({slider}=await openV2());fireEvent.keyDown(slider,{key:'End'});fireEvent.change(slider,{target:{value:'3'}});fireEvent.keyUp(slider,{key:'End'});
    expect(onChange).toHaveBeenLastCalledWith('gpt','gpt-5.6-luna','max');expect(onChange).toHaveBeenCalledTimes(2);
  });
  it('returns from model search to the slider without committing and still selects a legal model level', async () => {
    const {onChange}=renderV2();await openV2();const user=userEvent.setup();
    await user.click(screen.getByRole('button',{name:/更换模型/}));
    expect(screen.queryByRole('slider')).not.toBeInTheDocument();const search=screen.getByRole('searchbox');
    await waitFor(()=>expect(search).toHaveFocus());await user.type(search,'flash');await user.keyboard('{Escape}');
    await waitFor(()=>expect(screen.getByRole('slider')).toHaveFocus());expect(onChange).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button',{name:/更换模型/}));expect(screen.getByRole('searchbox')).toHaveValue('flash');
    await user.click(screen.getByRole('option',{name:'选择模型 DeepSeek V4 Flash'}));
    expect(onChange).toHaveBeenCalledTimes(1);expect(onChange).toHaveBeenCalledWith('deepseek','deepseek-v4-flash','off');
  });
  it('cancels an in-flight preview when the request owner becomes pending and prevents repeat commits', async () => {
    const {onChange,view,props}=renderV2();const {slider}=await openV2();
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'3'}});
    view.rerender(<ChatPresentationProvider ownerKey="test:reasoning-slider-v2" defaultVersion="v2"><ModelPicker {...props} pending/></ChatPresentationProvider>);
    expect(slider).toBeDisabled();expect(slider).toHaveValue('2');pointer(slider,'pointerup');
    expect(onChange).not.toHaveBeenCalled();expect(screen.getByRole('button',{name:/^模型与推理/})).toBeDisabled();
  });
  it('uses the portable Pi catalog levels without inventing Ultra or a four-level mapping', async () => {
    const onChange=vi.fn();render(<ChatPresentationProvider ownerKey="test:portable-slider" defaultVersion="v2"><ModelPicker disabled={false} pending={false} requestOpen={0} onChange={onChange}
      options={{modelReference:'custom/pi',thinking:'minimal',models:[{reference:'custom/pi',provider:'custom',id:'pi',name:'Pi model',thinkingLevels:['off','minimal','low','medium','high','xhigh','max','Ultra']}]}}/></ChatPresentationProvider>);
    await userEvent.setup().click(screen.getByRole('button',{name:/^模型与推理/}));const slider=screen.getByRole('slider');
    expect(slider).toHaveAttribute('max','6');expect(slider).toHaveValue('1');
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'5'}});pointer(slider,'pointerup');
    expect(onChange).toHaveBeenCalledWith('custom','pi','xhigh');
  });
});

describe('migrated max decoration lifecycle', () => {
  it('draws the migrated field and a single preview burst without selection, then cancels RAF on pointer cancellation', async () => {
    const {onChange}=renderV2();const {slider}=await openV2();vi.useFakeTimers();
    const frames=new Map<number,FrameRequestCallback>();let id=0;
    vi.spyOn(window,'requestAnimationFrame').mockImplementation(cb=>{frames.set(++id,cb);return id;});
    const cancel=vi.spyOn(window,'cancelAnimationFrame').mockImplementation(key=>{frames.delete(key);});
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'3'}});
    const effects=document.querySelector<HTMLElement>('.agent-reasoning-effects')!;
    expect(effects).toHaveAttribute('data-running','true');expect(onChange).not.toHaveBeenCalled();
    const draw=frames.get(id)!;act(()=>draw(34));expect(effects).toHaveAttribute('data-frames','1');
    act(()=>vi.advanceTimersByTime(220));expect(effects).toHaveAttribute('data-bursts','1');
    act(()=>vi.advanceTimersByTime(900));expect(effects).toHaveAttribute('data-bursts','1');
    const scheduled=id, late=frames.get(scheduled)!;pointer(slider,'pointercancel');
    expect(cancel).toHaveBeenCalledWith(scheduled);expect(effects).toHaveAttribute('data-running','false');
    act(()=>late(1234));expect(effects).toHaveAttribute('data-frames','1');expect(onChange).not.toHaveBeenCalled();
  });
  it('cancels RAF and pending burst when the real visibility owner becomes hidden, and does not replay entry on visibility return', async () => {
    const {onChange}=renderV2();const {slider}=await openV2();vi.useFakeTimers();
    let last:FrameRequestCallback|undefined,id=0;
    vi.spyOn(window,'requestAnimationFrame').mockImplementation(cb=>{last=cb;return ++id;});
    const cancel=vi.spyOn(window,'cancelAnimationFrame');const setTimer=vi.spyOn(window,'setTimeout');const clearTimer=vi.spyOn(window,'clearTimeout');
    pointer(slider,'pointerdown');fireEvent.change(slider,{target:{value:'3'}});
    const effects=document.querySelector<HTMLElement>('.agent-reasoning-effects')!,frame=id;
    const timerIndex=setTimer.mock.calls.findIndex(args=>args[1]===220),timer=setTimer.mock.results[timerIndex].value;
    const late=last!;vi.spyOn(document,'visibilityState','get').mockReturnValue('hidden');
    act(()=>document.dispatchEvent(new Event('visibilitychange')));
    expect(effects).toHaveAttribute('data-enabled','false');expect(cancel).toHaveBeenCalledWith(frame);expect(clearTimer).toHaveBeenCalledWith(timer);
    act(()=>late(100));expect(effects).toHaveAttribute('data-frames','0');
    vi.spyOn(document,'visibilityState','get').mockReturnValue('visible');act(()=>document.dispatchEvent(new Event('visibilitychange')));
    act(()=>vi.advanceTimersByTime(500));expect(effects).toHaveAttribute('data-bursts','0');expect(onChange).not.toHaveBeenCalled();
  });
});
