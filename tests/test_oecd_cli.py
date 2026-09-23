import unittest

import oecd_cli_source as cli


class OECDCLITests(unittest.TestCase):
    def test_four_regime_classifications(self):
        self.assertEqual(cli.classify_month(100.1, 100.2), 'expansion')
        self.assertEqual(cli.classify_month(100.2, 100.1), 'slowdown')
        self.assertEqual(cli.classify_month(99.9, 99.8), 'contraction')
        self.assertEqual(cli.classify_month(99.8, 99.9), 'recovery')

    def test_boundary_values_have_no_regime(self):
        self.assertIsNone(cli.classify_month(99.9, 100))
        self.assertIsNone(cli.classify_month(100.1, 100.1))
        self.assertIsNone(cli.classify_month(None, 99.9))
        self.assertIsNone(cli.classify_month(99.9, None))

    def test_missing_month_is_not_bridged(self):
        points = [
            ['2026-01-01', 99.0],
            ['2026-02-01', 98.0],
            ['2026-04-01', 100.1],
            ['2026-05-01', 100.2],
        ]
        self.assertEqual(
            cli.calculate_regimes(points),
            [
                ['2026-02-01', '2026-03-01', 'contraction'],
                ['2026-05-01', '2026-06-01', 'expansion'],
            ],
        )

    def test_adjacent_equal_regimes_are_merged(self):
        points = [
            ['2026-01-01', 99.0],
            ['2026-02-01', 99.2],
            ['2026-03-01', 99.4],
            ['2026-04-01', 99.6],
        ]
        self.assertEqual(
            cli.calculate_regimes(points),
            [['2026-02-01', '2026-05-01', 'recovery']],
        )

    def test_latest_month_is_not_extended_forward(self):
        points = [
            ['2026-07-01', 100.1],
            ['2026-08-01', 100.2],
        ]
        self.assertEqual(
            cli.calculate_regimes(points),
            [['2026-08-01', '2026-09-01', 'expansion']],
        )

    def test_fred_csv_preserves_missing_values(self):
        text = (
            'observation_date,USALOLITOAASTSAM\n'
            '2026-07-01,100.1\n'
            '2026-08-01,.\n'
        )
        self.assertEqual(
            cli.parse_fred_csv(text, 'USALOLITOAASTSAM'),
            [
                ['2026-07-01', 100.1],
                ['2026-08-01', None],
            ],
        )

    def test_korea_cli_series_is_available(self):
        self.assertEqual(cli.SERIES['kr'], 'KORLOLITOAASTSAM')


if __name__ == '__main__':
    unittest.main()
