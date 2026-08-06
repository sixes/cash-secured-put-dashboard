import pytest

from app.metrics.chart import PAD_B, PAD_L, PAD_R, PAD_T, build_price_chart


def days(n, start=1):
    """`n` consecutive ISO dates. Calendar correctness does not matter here — the x axis
    is one step per session — but the labels must stay sortable and parseable."""
    return [f"2020-{1 + (start + i) // 28:02d}-{1 + (start + i) % 28:02d}" for i in range(n)]


def lines(chart):
    return {ln.cls: ln for ln in chart.lines}


def points(chart, cls):
    return [
        tuple(float(v) for v in p.split(","))
        for p in lines(chart)[cls].points.split()
    ]


class TestGuards:
    def test_a_single_close_is_not_a_chart(self):
        assert build_price_chart(["2020-01-01"], [100.0]) is None

    def test_two_closes_are_enough(self):
        c = build_price_chart(days(2), [100.0, 101.0])
        assert c is not None and c.shown == 2

    def test_unusable_closes_are_dropped_with_their_dates(self):
        # A NaN or zero mid-series would otherwise draw a crash to zero.
        c = build_price_chart(
            ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"],
            [100.0, float("nan"), 0.0, 104.0],
        )
        assert c.n == 2
        assert c.first_date == "2020-01-01" and c.last_date == "2020-01-04"

    def test_a_flat_series_does_not_divide_by_zero(self):
        c = build_price_chart(days(5), [50.0] * 5)
        assert c is not None
        # All five points land on one horizontal line inside the plot area.
        ys = {y for _, y in points(c, "close")}
        assert len(ys) == 1
        assert c.plot_top < ys.pop() < c.plot_bottom


class TestAxes:
    def test_x_increases_across_the_plot_area(self):
        c = build_price_chart(days(50), [float(100 + i) for i in range(50)])
        xs = [x for x, _ in points(c, "close")]
        assert xs == sorted(xs)
        assert xs[0] == pytest.approx(PAD_L)
        assert xs[-1] == pytest.approx(c.width - PAD_R)

    def test_y_is_inverted_because_svg_grows_downward(self):
        # The single most likely bug in the whole module: the highest close must get the
        # SMALLEST y, or the chart renders upside down.
        c = build_price_chart(days(3), [100.0, 300.0, 200.0])
        _, mid, last = points(c, "close")
        assert mid[1] < last[1]
        assert mid[1] == pytest.approx(PAD_T)
        assert c.plot_bottom == pytest.approx(c.height - PAD_B)

    def test_the_extremes_touch_the_plot_edges(self):
        c = build_price_chart(days(4), [10.0, 20.0, 15.0, 12.0])
        ys = [y for _, y in points(c, "close")]
        assert min(ys) == pytest.approx(c.plot_top)
        assert max(ys) == pytest.approx(c.plot_bottom)

    def test_y_ticks_span_the_value_range_and_map_to_their_own_y(self):
        c = build_price_chart(days(3), [10.0, 20.0, 15.0])
        values = [v for v, _ in c.y_ticks]
        assert values[0] == pytest.approx(10.0) and values[-1] == pytest.approx(20.0)
        # Each tick's y must be the y its own value maps to, else the gridlines lie.
        assert c.y_ticks[0][1] == pytest.approx(c.plot_bottom)
        assert c.y_ticks[-1][1] == pytest.approx(c.plot_top)

    def test_x_ticks_are_year_month_labels_at_real_x_positions(self):
        c = build_price_chart(days(40), [float(100 + i) for i in range(40)])
        assert all(len(label) == 7 and label[4] == "-" for label, _ in c.x_ticks)
        assert c.x_ticks[0][1] == pytest.approx(PAD_L)
        assert c.x_ticks[-1][1] == pytest.approx(c.width - PAD_R)


class TestRunningHigh:
    def test_the_high_never_decreases(self):
        closes = [10.0, 30.0, 20.0, 25.0, 40.0, 35.0]
        c = build_price_chart(days(6), closes)
        # y descends as price rises, so a non-decreasing high is a non-increasing y.
        ys = [y for _, y in points(c, "high")]
        assert ys == sorted(ys, reverse=True)

    def test_only_the_risers_are_emitted(self):
        # 200 flat sessions, one new high, then flat again: a staircase of
        # start / riser corner / riser top / end, not 206 collinear points.
        closes = [100.0] * 200 + [150.0] * 6
        c = build_price_chart(days(206), closes)
        assert len(points(c, "high")) == 4

    def test_a_riser_on_the_final_session_is_not_duplicated(self):
        c = build_price_chart(days(201), [100.0] * 200 + [150.0])
        assert len(points(c, "high")) == 3

    def test_the_riser_is_square_not_a_diagonal_ramp(self):
        c = build_price_chart(days(3), [10.0, 20.0, 20.0])
        pts = points(c, "high")
        # Two points share the riser's x: the old level and the new one.
        xs = [x for x, _ in pts]
        assert xs.count(pts[1][0]) == 2
        assert pts[1][1] != pts[2][1]

    def test_the_high_is_expanding_over_the_whole_series_not_the_window(self):
        # Peak at 500 falls outside a 10-session window. A windowed max would reset to
        # 120 and report a 0% drawdown from a high the ticker is nowhere near.
        closes = [500.0] + [100.0] * 8 + [120.0] * 12
        c = build_price_chart(days(21), closes, window=10)
        assert c.shown == 10
        assert c.high == pytest.approx(500.0)
        assert c.drawdown_pct == pytest.approx((120 / 500 - 1) * 100)
        # The staircase entering the window is already at 500 and stays there.
        assert len({y for _, y in points(c, "high")}) == 1

    def test_the_high_date_is_when_it_was_first_set(self):
        d = days(5)
        c = build_price_chart(d, [10.0, 90.0, 20.0, 90.0, 30.0])
        assert c.high == pytest.approx(90.0)
        assert c.high_date == d[1]


class TestSma:
    def test_no_sma_line_below_the_window(self):
        c = build_price_chart(days(137), [float(100 + i) for i in range(137)], sma_window=200)
        assert "sma" not in lines(c)
        assert c.sma is None

    def test_sma_matches_the_hand_computed_mean(self):
        # closes 1..200 -> trailing 200-mean is 100.5
        c = build_price_chart(days(200), [float(i) for i in range(1, 201)], sma_window=200)
        assert c.sma == pytest.approx(100.5)

    def test_sma_is_computed_over_the_whole_series_so_the_line_reaches_the_left_edge(self):
        # 250 sessions, 50 drawn. A window-local SMA would have no value at all.
        c = build_price_chart(days(250), [float(i) for i in range(1, 251)], sma_window=200, window=50)
        sma = points(c, "sma")
        assert len(sma) == 50
        assert sma[0][0] == pytest.approx(PAD_L)

    def test_a_partial_sma_is_never_drawn(self):
        # 210 sessions with a 200 window: the first 199 have no mean, so the line starts
        # at the 200th session rather than showing a shorter average as an SMA200.
        c = build_price_chart(days(210), [float(i) for i in range(1, 211)], sma_window=200)
        assert len(points(c, "sma")) == 11

    def test_the_sma_is_inside_the_y_range(self):
        c = build_price_chart(days(210), [float(i) for i in range(1, 211)], sma_window=200)
        ys = [y for _, y in points(c, "sma")]
        assert min(ys) >= c.plot_top and max(ys) <= c.plot_bottom


class TestProvenance:
    def test_the_window_reports_what_it_actually_drew(self):
        d = days(300)
        c = build_price_chart(d, [float(100 + i) for i in range(300)], window=100)
        assert c.n == 300 and c.shown == 100
        assert c.first_date == d[0]
        assert c.window_first_date == d[200]
        assert c.last_date == d[-1]

    def test_a_shorter_series_than_the_window_draws_all_of_it(self):
        c = build_price_chart(days(30), [float(100 + i) for i in range(30)], window=756)
        assert c.shown == 30

    def test_source_labels_pass_through_untouched(self):
        c = build_price_chart(
            days(3), [1.0, 2.0, 3.0], source="yahoo", source_label="Yahoo adj close", all_time=True
        )
        assert (c.source, c.source_label, c.all_time) == ("yahoo", "Yahoo adj close", True)

    def test_all_time_defaults_off_so_a_window_high_is_never_called_an_ath(self):
        assert build_price_chart(days(3), [1.0, 2.0, 3.0]).all_time is False
