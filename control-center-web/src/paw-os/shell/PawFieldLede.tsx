import { ArrowRight } from 'lucide-react';
import { memo } from 'react';
import type { PawAppId } from '../runtime/app-registry';
import { PawBrandMark } from './PawAppIcon';

/**
 * PawFieldLede — 项目场的开场白，桌面第一屏的主体。
 *
 * 第一屏原本只有两块相同的白色角落面板和一片空掉的中间。这里补上主体：一段
 * 说明这台机器是做什么的开场白，和唯一一个主动作。它与 Agent 首页共用同一套
 * 品牌语法（钴蓝短杠的眉标、重字重标题、标题里一处钴蓝 em），所以桌面和 App
 * 读起来是同一个产品，而不是两套设计。
 *
 * 标题说结果，副句说这个结果是怎么来的：三个加重的词就是那条路径上真实存在的
 * 三样东西。标题刻意不与 Agent 首页的「交给 Agent 一件事。」同句——按钮通向那
 * 一屏，两句连起来读是一条路，重复一遍就只是回声。
 *
 * 边界：
 * - 只做导航，不持有任何状态、不发请求、不缓存任何进度。项目场是进入工作的
 *   入口，永远不是开始 Session 的前置条件（PROJECT.md 非目标）。
 * - 文案只说得出真实产品名词（Session、回执、记忆与知识、上下文），不写指标、
 *   不写连接状态、不替 Runtime 作任何声明。
 * - `data-paw-evidence-echo` 是双向证据链在桌面上的锚点。轨迹节点 ↔ 条目页
 *   的来回已在 Agent 轨迹与 Memory / Knowledge / Files 落地；这里保持可寻址
 *   但不持有它——桌面仍然只做导航，副句里的「回执 → 记忆与知识 → 上下文」
 *   就是那条链路在文案上的同一句话。
 *
 * 样式：paw-os.css 拥有几何，paw-os-shell.css 拥有着墨与雾面底。
 */
export const PawFieldLede = memo(function PawFieldLede({ onOpen }: { onOpen: (appId: PawAppId) => void }) {
  return (
    <div className="paw-field-lede" data-paw-evidence-echo="field-lede">
      <p className="paw-field-lede__mark">
        <PawBrandMark size={13} />
        <b className="paw-brand-wordmark">PAW</b>
        <span>项目场</span>
      </p>
      <h2 className="paw-field-lede__title">做过的事，下次<em>不用重讲</em>。</h2>
      <p className="paw-field-lede__copy">
        交给 Session 一件事；做完留下<b>回执</b>，被接受的那部分进入
        <b>记忆与知识</b>，下一次工作从更短的<b>上下文</b>开始。
      </p>
      <button className="paw-field-lede__start" onClick={() => onOpen('agent')} type="button">
        开始一件事<ArrowRight aria-hidden="true" size={15} />
      </button>
    </div>
  );
});
