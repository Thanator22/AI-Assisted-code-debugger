# Bulk discount missing at threshold

## Description

Customers purchasing exactly 10 units are not receiving the advertised
10% bulk discount. The discount works when more than 10 units are purchased.

## Steps to reproduce

1. Set the unit price to 100.
2. Set the quantity to 10.
3. Calculate the order total.

## Expected behavior

The subtotal is 1000. A 10% discount should be applied, producing a total
of 900.

## Actual behavior

The calculated total is 1000.

## Acceptance criteria

- A quantity of exactly 10 receives the 10% discount.
- Quantities greater than 10 continue receiving the discount.
- Quantities below 10 do not receive the discount.
- Existing input validation continues to work.