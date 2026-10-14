# Average calculation loses fractional values

AverageService returns a whole-number result when the mathematical
average contains a fractional part.

Examples:
- [1, 2] should return 1.5.
- [-1, -2] should return -1.5.

Preserve support for large integer values and the existing rejection
of null or empty input.