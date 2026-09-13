import { expect, it } from 'vitest';
import { parseViewCommand } from './view-contract';

it('accepts real map and panel operations with stable request identity', () => {
  expect(parseViewCommand({requestId:'one',runId:'run',action:'focus',center:[120,30],zoom:14})).toMatchObject({requestId:'one',runId:'run'});
  expect(parseViewCommand({requestId:'two',action:'layer',layerId:'terrain',visible:false}).visible).toBe(false);
  expect(parseViewCommand({requestId:'three',action:'panel',panel:'sources'}).panel).toBe('sources');
});
it('rejects invalid geography and incomplete or unknown operations', () => {
  for (const command of [
    {action:'focus',center:[190,30],zoom:14},
    {action:'focus',center:[120,30],zoom:NaN},
    {action:'layer',layerId:'terrain'},
    {action:'feature',featureId:''},
    {action:'panel',panel:'delete'},
  ]) expect(() => parseViewCommand({requestId:'x',...command})).toThrow();
});
