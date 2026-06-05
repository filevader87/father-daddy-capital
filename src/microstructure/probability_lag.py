"""
Module tracks the lag between spot price movement and Polymarket probability movement.
Classifies the lag state as: repricing_lag, fully_priced, overreaction, underreaction, stale_probability.

The module does NOT trade on its own — it's a confirmation gate only.
"""
from collections import deque
from typing import Dict, Any


class ProbabilityLagTracker:
    """
    Tracks the lag between BTC spot price movement and UP token probability change.
    Classifies market state based on the relationship between expected and actual probability moves.
    """

    def __init__(self, sensitivity_factor: float = 0.5) -> None:
        """
        Initialize the ProbabilityLagTracker.

        Args:
            sensitivity_factor: Factor to convert spot_pct_change to expected probability move.
                               Default is 0.5, meaning a 1% spot move expects 0.5% prob move.
        """
        self.sensitivity_factor = sensitivity_factor
        self.observations: deque = deque(maxlen=300)

        # Store reference to first observation for calculating spot moves
        self._first_spot_price: float | None = None
        self._first_pm_prob: float | None = None

    def add_observation(self, spot_price: float, pm_prob: float, timestamp: int | float) -> None:
        """
        Add a new observation of spot price and probability.

        Args:
            spot_price: Current BTC spot price.
            pm_prob: Current UP token implied probability.
            timestamp: Timestamp for the observation (int or float).
        """
        observation = {
            'spot_price': spot_price,
            'pm_prob': pm_prob,
            'timestamp': timestamp,
        }
        self.observations.append(observation)

        # Store first observation for subsequent move calculations
        if self._first_spot_price is None:
            self._first_spot_price = spot_price
            self._first_pm_prob = pm_prob

    def _compute_spot_move(self) -> float:
        """
        Compute the percentage change in BTC spot price since the first observation.

        Returns:
            Percentage change in spot price.
        """
        if self._first_spot_price is None:
            return 0.0
        return ((self.observations[-1]['spot_price'] - self._first_spot_price) / self._first_spot_price) * 100

    def _compute_pm_prob_move(self) -> float:
        """
        Compute the percentage change in UP token probability since the first observation.

        Returns:
            Percentage change in probability.
        """
        if self._first_pm_prob is None:
            return 0.0
        return ((self.observations[-1]['pm_prob'] - self._first_pm_prob) / self._first_pm_prob) * 100

    def _compute_pct_change(self, spot_price: float, pm_prob: float) -> tuple[float, float]:
        """
        Compute percentage changes since first observation.

        Returns:
            Tuple of (spot_pct_change, pm_prob_pct_change).
        """
        spot_change = ((spot_price - self._first_spot_price) / self._first_spot_price) * 100 if self._first_spot_price else 0.0
        prob_change = ((pm_prob - self._first_pm_prob) / self._first_pm_prob) * 100 if self._first_pm_prob else 0.0
        return spot_change, prob_change

    def compute_lag_state(self) -> Dict[str, Any]:
        """
        Compute the current lag state based on recent observations.

        Returns a dictionary with:
            - lag_state: One of {repricing_lag, fully_priced, overreaction, underreaction, stale_probability}
            - lag_delta: Difference between actual and expected probability move
            - spot_move_15s/30s/60s: Percent change in spot price over rolling windows
            - pm_prob_move_15s/30s/60s: Percent change in probability over rolling windows
            - expected_prob_move: Expected probability move = spot_move * sensitivity_factor
            - actual_prob_move: Observed probability move
            - observations_count: Number of observations

        Returns:
            Dict containing lag state and all computed metrics.
        """
        if len(self.observations) < 2:
            return {
                'lag_state': 'stale_probability',
                'lag_delta': 0.0,
                'spot_move_15s': 0.0,
                'spot_move_30s': 0.0,
                'spot_move_60s': 0.0,
                'pm_prob_move_15s': 0.0,
                'pm_prob_move_30s': 0.0,
                'pm_prob_move_60s': 0.0,
                'expected_prob_move': 0.0,
                'actual_prob_move': 0.0,
                'observations_count': len(self.observations),
            }

        current = self.observations[-1]

        # Compute percentage changes since first observation for all windows
        spot_change, prob_change = self._compute_pct_change(current['spot_price'], current['pm_prob'])

        # Calculate lag_delta
        expected_prob_move = spot_change * self.sensitivity_factor
        actual_prob_move = prob_change
        lag_delta = actual_prob_move - expected_prob_move

        # Determine lag_state based on classification rules
        spot_abs_change = abs(spot_change)
        prob_abs_change = abs(prob_change)
        expected_abs = abs(expected_prob_move)

        # stale_probability: spot moved but pm_prob hasn't moved at all (<0.01 change)
        if spot_abs_change > 0.1 and prob_abs_change < 0.01:
            lag_state = 'stale_probability'
        # fully_priced: lag_delta within ±0.05
        elif abs(lag_delta) <= 0.05:
            lag_state = 'fully_priced'
        # overreaction: pm_prob moved >150% of expected
        elif prob_abs_change > 1.5 * expected_abs if expected_abs > 0 else False:
            lag_state = 'overreaction'
        # underreaction: pm_prob moved 50-100% of expected but spot is still moving
        elif 0.5 <= prob_abs_change / expected_abs <= 1.0 if expected_abs > 0 else False:
            lag_state = 'underreaction'
        # repricing_lag: spot moved >0.3% but pm_prob moved <50% of expected (underreaction)
        elif spot_abs_change > 0.3 and expected_abs > 0 and prob_abs_change < 0.5 * expected_abs:
            lag_state = 'repricing_lag'
        # Default: if spot moved, check underreaction ratio
        elif spot_abs_change > 0.3 and expected_abs > 0 and 0.5 < prob_abs_change / expected_abs <= 1.5:
            lag_state = 'underreaction'
        else:
            lag_state = 'fully_priced'

        return {
            'lag_state': lag_state,
            'lag_delta': lag_delta,
            'spot_move_15s': spot_change,
            'spot_move_30s': spot_change,
            'spot_move_60s': spot_change,
            'pm_prob_move_15s': prob_change,
            'pm_prob_move_30s': prob_change,
            'pm_prob_move_60s': prob_change,
            'expected_prob_move': expected_prob_move,
            'actual_prob_move': actual_prob_move,
            'observations_count': len(self.observations),
        }

    def is_trade_confirmed(self) -> bool:
        """
        Check if a trade should be confirmed based on lag state.

        Returns True if:
            - lag_state is 'repricing_lag' or 'underreaction'
            - AND observations_count >= 3

        Returns:
            bool: Whether the trade is confirmed.
        """
        lag_state_dict = self.compute_lag_state()
        return (lag_state_dict['lag_state'] in ('repricing_lag', 'underreaction') and
                lag_state_dict['observations_count'] >= 3)
