import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, modules):
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(f'test_{name}', ROOT/'tools'/f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def stubs(*names, **values):
    """Empty modules for the pinned upstream imports, plus explicit attributes."""
    modules = {}
    for name in names:
        modules[name] = types.ModuleType(name)
    for dotted, value in values.items():
        module, attr = dotted.rsplit('__', 1)
        setattr(modules[module.replace('__', '.')], attr, value)
    return modules


class FakeState:
    """The gameplay.State subset restore_records uses, over in-memory tables."""
    def __init__(self, tables):
        self.store = tables
        self.tables = {}
        self.updated = []

    async def rows(self, name):
        if name not in self.tables:
            self.tables[name] = [dict(r) for r in self.store[name]]
        return self.tables[name]

    async def one(self, name, **where):
        return next((r for r in await self.rows(name) if all(r.get(k) == v for k, v in where.items())), None)

    async def update(self, entity, row, **values):
        row.update(values)
        self.updated.append((entity, values))


class AutoplayRecords(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        names = ('gameplay', 'helpers', 'helpers.cache', 'helpers.msgpack', 'helpers.live', 'helpers.live_result',
                 'helpers.score', 'helpers.live_rate', 'models', 'routes', 'routes.lives', 'scripts', 'scripts._sirius')
        modules = stubs(*names)
        for attr in ('State', 'Rejected', 'master', 'transaction'):
            setattr(modules['gameplay'], attr, Mock())
        modules['helpers.cache'].cache = Mock()
        for attr in ('respond', 'read_request', 'from_array', 'fault'):
            setattr(modules['helpers.msgpack'], attr, Mock())
        modules['helpers.live'].build_live_time_event = Mock()
        modules['helpers.live_result'].play_totals = Mock()
        modules['helpers.score'].verify_score_blocks = Mock()
        for attr in ('StartLivePayload', 'FinishLivePayload', 'FinishLiveResult', 'ConcertResult'):
            setattr(modules['models'], attr, Mock())
        modules['routes'].lives = modules['routes.lives']
        modules['scripts._sirius']._unpack_all = Mock()
        modules['scripts._sirius']._decompress = Mock()
        # pairs reach upstream total_rate as (live master, rate); sum rates to see what passed
        modules['helpers.live_rate'].total_rate = lambda pairs: sum(rate for _, rate in pairs)
        self.modules = modules
        self.live = load('live_modes', modules)

    def state(self, live, music=((1, False, 0),), rate=167.05):
        return FakeState({
            'Live': [dict(liveMasterId=25205, timesCompleted=5, **live)],
            'Music': [dict(id=i, stellaReleased=s, olivierReleaseStatus=o) for i, s, o in music],
            'UserProfile': [dict(id=1, playerRate=rate)],
        })

    async def test_autoplay_keeps_previous_best_and_counts_the_play(self):
        best = dict(achievementRate=99.5, notationRate=12.3, clearLamp=2, rateGrade=4)
        s = self.state(best)
        before = await self.live.record_snapshot(s, 25205)
        # what upstream saved for the autoplay run
        s.store['Live'][0].update(achievementRate=101.0, notationRate=18.0, clearLamp=6, rateGrade=9, timesCompleted=6)
        s.store['Music'][0].update(stellaReleased=True)
        s.store['UserProfile'][0].update(playerRate=170.0)
        result = SimpleNamespace(is_high_score=True, rate_result=SimpleNamespace(total_rate_before=167.05, total_rate_after=170.0))
        await self.live.restore_records(s, before, 25205, result)
        live = await s.one('Live', liveMasterId=25205)
        self.assertEqual({k: live[k] for k in best}, best)
        self.assertEqual(live['timesCompleted'], 6)
        self.assertFalse((await s.one('Music', id=1))['stellaReleased'])
        self.assertEqual((await s.one('UserProfile'))['playerRate'], 167.05)
        self.assertFalse(result.is_high_score)
        self.assertEqual(result.rate_result.total_rate_after, 167.05)

    async def test_autoplay_on_new_chart_leaves_no_best(self):
        s = FakeState({'Live': [], 'Music': [], 'UserProfile': [dict(id=1, playerRate=10.0)]})
        before = await self.live.record_snapshot(s, 25205)
        s.store['Live'].append(dict(liveMasterId=25205, timesCompleted=1, achievementRate=101.0, notationRate=18.0, clearLamp=6, rateGrade=9))
        result = SimpleNamespace(is_high_score=True, rate_result=None)
        await self.live.restore_records(s, before, 25205, result)
        live = await s.one('Live', liveMasterId=25205)
        self.assertEqual((live['achievementRate'], live['clearLamp'], live['rateGrade'], live['timesCompleted']), (0.0, 0, 0, 1))

    def test_unplayed_charts_do_not_count_toward_player_rate(self):
        with patch.dict(sys.modules, self.modules):
            self.assertEqual(self.live.played_total_rate([(1, 0.0), (2, 99.0), (3, 0)]), 99.0)


class DualCards(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        masters = {
            150050: SimpleNamespace(id_=150050, character_base_master_id=102, secondary_character_base_master_id=101),
            110010: SimpleNamespace(id_=110010, character_base_master_id=101, secondary_character_base_master_id=None),
        }
        self.bases = {101: 9001, 102: 9002}  # owned CharacterBase id per idol
        ids = iter(range(500, 600))
        modules = stubs('helpers', 'helpers.cache', 'helpers.things', 'db', 'db.user', 'db.user.grant')
        modules['helpers.cache'].cache = SimpleNamespace(character_master=list(masters.values()))
        modules['helpers.things']._by_id = lambda table: masters if table == 'character_master' else {}
        modules['helpers.things']._random_id = lambda: next(ids)
        modules['db.user.grant'].get_character_base = lambda uid, base: ('base', base)
        modules['db.user.grant'].create_character_base = lambda *a: ('create', a)
        self.modules = modules
        self.dual = load('dual_card_compat', modules)
        self.conn = Mock()
        self.conn.fetchrow = AsyncMock(side_effect=lambda q: SimpleNamespace(id=self.bases[q[1]]) if q[1] in self.bases else None)
        self.conn.execute = AsyncMock()
        self.conn.conn = Mock(execute=AsyncMock(), fetch=AsyncMock(return_value=[]))

    async def call(self, fn, *args):
        with patch.dict(sys.modules, self.modules):
            return await fn(*args)

    async def test_grant_links_the_second_idol(self):
        await self.call(self.dual.grant_character, self.conn, 1, 150050)
        args = self.conn.conn.execute.await_args.args
        self.assertIn('"secondaryCharacterBaseId"', args[0])
        self.assertEqual(args[1:], (1, 500, 150050, 9002, 9001, 1))

    async def test_grant_single_idol_card_is_unchanged(self):
        await self.call(self.dual.grant_character, self.conn, 1, 110010)
        self.assertEqual(self.conn.conn.execute.await_args.args[1:], (1, 500, 110010, 9001, None, 0))

    async def test_second_idol_base_is_created_when_missing(self):
        del self.bases[101]
        await self.call(self.dual.grant_character, self.conn, 1, 150050)
        self.conn.execute.assert_awaited_once_with(('create', (1, 500, 101, None, 150050)))
        self.assertEqual(self.conn.conn.execute.await_args.args[5], 500)

    async def test_repair_fills_saved_dual_cards(self):
        self.conn.conn.fetch.return_value = [{'userId': 1, 'id': 4848299183, 'characterMasterId': 150050}]
        self.assertEqual(await self.call(self.dual.repair_dual_cards, self.conn), 1)
        self.assertEqual(self.conn.conn.fetch.await_args.args[1], [150050])
        self.assertEqual(self.conn.conn.execute.await_args.args[1:], (1, 4848299183, 9001))


if __name__ == '__main__':
    unittest.main()
