package com.example.discount;

public class DiscountService {

    private static final int BULK_DISCOUNT_PERCENTAGE = 10;
    private static final int BULK_DISCOUNT_THRESHOLD = 10;

    public int calculateTotal(int unitPrice, int quantity) {
        if (unitPrice <= 0) {
            throw new IllegalArgumentException(
                    "Unit price must be positive"
            );
        }

        if (quantity <= 0) {
            throw new IllegalArgumentException(
                    "Quantity must be positive"
            );
        }

        int subtotal = unitPrice * quantity;

        if (quantity > BULK_DISCOUNT_THRESHOLD) {
            int discount =
                    subtotal * BULK_DISCOUNT_PERCENTAGE / 100;

            return subtotal - discount;
        }

        return subtotal;
    }
}