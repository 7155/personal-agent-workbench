import type { CSSProperties } from 'react';
import type { StellarAgentProjection } from './stellar-agent-projection';
import './stellar-agent-field.css';

// Existing locally shipped photographic maps. A palette is identity only;
// planet size/position never stands for cost, progress or model capability.
const surfaces = ['earth', 'mars', 'jupiter', 'neptune', 'venus', 'saturn'];
const base = import.meta.env.BASE_URL || '/';
const texture = (index: number) => new URL(
  `${base}paw-media/starfield/${surfaces[index % surfaces.length]}-1k.jpg`,
  document.baseURI,
).href;

export function StellarAgentField({ projection }: { projection: StellarAgentProjection }) {
  const planets = projection.runningPlanets;
  if (!planets.length) return null;
  const position = (planet: typeof planets[number]) => ({
    x: 20 + planet.position.x * 69,
    y: 37 + planet.position.y * 45,
  });
  const positions = new Map(planets.map((planet) => [planet.sessionId, position(planet)]));
  return (
    <div className="paw-stellar-agents" data-running-count={planets.length}>
      <svg aria-hidden="true" className="paw-stellar-agents__links" preserveAspectRatio="none" viewBox="0 0 100 100">
        {projection.relationships.map((link) => {
          const from = positions.get(link.fromSessionId);
          const to = positions.get(link.toSessionId);
          if (!from || !to) return null;
          return <line key={`${link.fromSessionId}:${link.toSessionId}`} x1={from.x} y1={from.y} x2={to.x} y2={to.y} />;
        })}
      </svg>
      {planets.map((planet) => {
        const point = position(planet);
        const palette = planet.style.paletteIndex;
        const size = 78 + planet.style.seed % 65;
        return (
          <div
            className="paw-stellar-agent"
            data-agent-session={planet.sessionId}
            data-running="true"
            key={planet.sessionId}
            style={{
              left: `${point.x}%`, top: `${point.y}%`,
              '--stellar-body-size': `${size}px`,
              '--stellar-texture': `url("${texture(palette)}")`,
              '--stellar-delay': `${-(planet.style.seed % 28)}s`,
              '--stellar-tilt': `${planet.style.seed % 35 - 17}deg`,
            } as CSSProperties}
          >
            <div className="paw-stellar-agent__float">
              <div className="paw-stellar-agent__orbit" />
              <div className="paw-stellar-agent__sphere"><div className="paw-stellar-agent__surface" /></div>
              <div className="paw-stellar-agent__caption">
                <strong>{planet.title}</strong>
                <span><i />{planet.kind === 'subagent' ? '子 Agent 运行中' : '运行中'}</span>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
