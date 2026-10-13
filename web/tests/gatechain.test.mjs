import test from 'node:test';
import assert from 'node:assert/strict';
import { mergeAnswers } from '../src/gatechain.js';

test('consent survives an intent answer and a later dice roll', () => {
  const consent = { consent_token: 'canary-consent' };
  const intent = { ability_intent: 'yes' };
  const answers = mergeAnswers(consent, intent);
  assert.deepEqual(answers, { consent_token: 'canary-consent', ability_intent: 'yes' });
  assert.deepEqual(mergeAnswers(answers, { dice_roll: 12 }), { consent_token: 'canary-consent', ability_intent: 'yes', dice_roll: 12 });
  assert.deepEqual(consent, { consent_token: 'canary-consent' });
  assert.deepEqual(intent, { ability_intent: 'yes' });
});

test('declining escalation survives an intent answer', () => {
  assert.deepEqual(mergeAnswers({ decline_escalation: true }, { ability_intent: 'yes' }), { decline_escalation: true, ability_intent: 'yes' });
});

test('missing answers are empty and later answers replace earlier values', () => {
  assert.deepEqual(mergeAnswers(), {});
  assert.deepEqual(mergeAnswers(null, { ability_intent: 'yes' }), { ability_intent: 'yes' });
  assert.deepEqual(mergeAnswers({ decline_escalation: true }, null), { decline_escalation: true });
  assert.deepEqual(mergeAnswers({ ability_intent: 'yes' }, { ability_intent: 'no' }), { ability_intent: 'no' });
});
