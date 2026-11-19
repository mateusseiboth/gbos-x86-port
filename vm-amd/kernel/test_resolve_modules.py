import unittest

from resolve_modules import Module, resolve


def mod(name, side, exports=None, needs=None):
    return Module(name=name, side=side, exports=dict(exports or {}), needs=dict(needs or {}))


class ResolveTest(unittest.TestCase):
    def test_google_wins_a_shared_name_when_everyone_is_satisfied(self):
        mods = [
            mod("helper.ko", "google", {"h": 1}),
            mod("helper.ko", "ours", {"h": 1}),
            mod("gpu.ko", "ours", needs={"h": 1}),
        ]
        chosen, dropped = resolve(mods, kernel={})
        self.assertEqual(chosen["helper.ko"], "google")
        self.assertEqual(dropped, set())

    def test_switches_to_ours_when_that_satisfies_both_sides(self):
        mods = [
            mod("ttm.ko", "google", {"t": 1}),
            mod("ttm.ko", "ours", {"t": 2}),
            mod("amdgpu.ko", "ours", needs={"t": 2}),
            mod("xe.ko", "google", needs={"t": 2}),  # Google's xe already matches our ttm
        ]
        chosen, dropped = resolve(mods, kernel={})
        self.assertEqual(chosen["ttm.ko"], "ours")
        self.assertEqual(dropped, set())

    def test_keeps_google_when_its_consumers_need_symbols_ours_lacks(self):
        mods = [
            mod("dp.ko", "google", {"dp": 1, "dsc": 1}),
            mod("dp.ko", "ours", {"dp": 1}),
            mod("xe.ko", "google", needs={"dp": 1, "dsc": 1}),
            mod("amdgpu.ko", "ours", needs={"dp": 1}),
        ]
        chosen, dropped = resolve(mods, kernel={})
        self.assertEqual(chosen["dp.ko"], "google")
        self.assertEqual(dropped, set())

    def test_drops_our_consumer_when_no_choice_satisfies_everyone(self):
        mods = [
            mod("p.ko", "google", {"s": 1}),
            mod("p.ko", "ours", {"s": 2}),
            mod("g.ko", "google", needs={"s": 1}),
            mod("o.ko", "ours", needs={"s": 2}),
        ]
        chosen, dropped = resolve(mods, kernel={})
        self.assertEqual(chosen["p.ko"], "google")
        self.assertEqual(dropped, {"o.ko"})

    def test_kernel_exports_satisfy_needs(self):
        chosen, dropped = resolve([mod("a.ko", "ours", needs={"k": 7})], kernel={"k": 7})
        self.assertEqual((chosen, dropped), ({"a.ko": "ours"}, set()))

    def test_dropping_cascades_to_dependents(self):
        mods = [
            mod("base.ko", "ours", {"b": 1}, needs={"missing": 1}),
            mod("top.ko", "ours", needs={"b": 1}),
        ]
        chosen, dropped = resolve(mods, kernel={})
        self.assertEqual(dropped, {"base.ko", "top.ko"})

    def test_google_module_broken_by_the_kernel_is_replaced_by_ours(self):
        # cfg80211: Google's needs a kernel CRC our core does not have; ours was built against it.
        mods = [
            mod("cfg80211.ko", "google", {"c": 1}, needs={"net_ns": 1}),
            mod("cfg80211.ko", "ours", {"c": 2}, needs={"net_ns": 2}),
            mod("iwlmvm.ko", "ours", needs={"c": 2}),
        ]
        chosen, dropped = resolve(mods, kernel={"net_ns": 2})
        self.assertEqual(chosen["cfg80211.ko"], "ours")
        self.assertEqual(dropped, set())

    def test_a_module_that_cannot_load_exports_nothing(self):
        # Google's mac80211 only "works" if Google's cfg80211 loads, and it cannot: switch both.
        mods = [
            mod("cfg80211.ko", "google", {"c": 1}, needs={"net_ns": 1}),
            mod("cfg80211.ko", "ours", {"c": 2}, needs={"net_ns": 2}),
            mod("mac80211.ko", "google", {"m": 1}, needs={"c": 1}),
            mod("mac80211.ko", "ours", {"m": 2}, needs={"c": 2}),
            mod("iwlmvm.ko", "ours", needs={"m": 2, "c": 2}),
        ]
        chosen, dropped = resolve(mods, kernel={"net_ns": 2})
        self.assertEqual((chosen["cfg80211.ko"], chosen["mac80211.ko"]), ("ours", "ours"))
        self.assertEqual(dropped, set())

    def test_google_modules_are_never_dropped_silently(self):
        mods = [mod("g.ko", "google", needs={"nothing": 1})]
        chosen, dropped = resolve(mods, kernel={})
        self.assertEqual(chosen["g.ko"], "google")
        self.assertEqual(dropped, set())


if __name__ == "__main__":
    unittest.main()
