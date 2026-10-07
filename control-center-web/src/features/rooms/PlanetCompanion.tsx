import { useEffect, useRef, useState } from 'react';
import { PlanetCompanionMark } from './PlanetCompanionMark';
import { PLANET_ACTIVITY_SIGNAL, type PlanetActivity, type PlanetExpression, type PlanetIdentity, type PlanetMotionMode, type PlanetSignalState } from './planet-companion-protocol';
import './planet-companion.css';

/** Display signals never change the face, own work, or replay a completion. */
export function PlanetCompanion(props: {
  name: PlanetIdentity; ordinal: number; size: number; idPrefix: string; className?: string; decorative?: boolean;
  activity: PlanetActivity; expression?: PlanetExpression; signalState?: PlanetSignalState; motion?: PlanetMotionMode; motionActive: boolean;
}) {
  const signal = props.signalState ?? PLANET_ACTIVITY_SIGNAL[props.activity];
  const mode = !props.motionActive || signal === 'offline' || !props.signalState && props.activity === 'static' ? 'static' : props.motion ?? 'full';
  const previous = useRef(signal);
  const doneSeen = useRef(signal === 'done');
  const observed = useRef(Boolean(props.signalState) || props.activity !== 'static');
  const [pulse, setPulse] = useState(false);
  useEffect(() => {
    // Unknown recovery and disconnect do not erase the last known signal.
    if ((!props.signalState && props.activity === 'static') || signal === 'offline') { observed.current = false; setPulse(false); return; }
    const entered = observed.current && previous.current !== signal;
    observed.current = true;
    previous.current = signal;
    if (signal === 'working') doneSeen.current = false;
    const repeatDone = signal === 'done' && doneSeen.current;
    if (signal === 'done') doneSeen.current = true;
    if (mode === 'static' || !entered || repeatDone || !['waiting', 'done', 'error'].includes(signal)) { setPulse(false); return; }
    setPulse(true);
    const timer = setTimeout(() => setPulse(false), 240);
    return () => clearTimeout(timer);
  }, [signal, mode, props.activity, props.signalState]);
  return <PlanetCompanionMark {...props} signalState={signal} motionMode={mode} statePulse={pulse && mode !== 'static'}/>;
}
