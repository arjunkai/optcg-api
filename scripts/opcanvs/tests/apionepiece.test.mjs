import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cleanEnString } from '../lib/apionepiece.mjs';

test('age suffix', () => assert.equal(cleanEnString('19 ans'), '19 years'));
test('status vivant', () => assert.equal(cleanEnString('vivant'), 'alive'));
test('status mort', () => assert.equal(cleanEnString('mort'), 'deceased'));
test('fruit type', () => assert.equal(cleanEnString('Zoan Mythique'), 'Mythical Zoan'));
test('already-english passes through', () => assert.equal(cleanEnString('living'), 'living'));
test('nullish -> empty', () => assert.equal(cleanEnString(null), ''));
