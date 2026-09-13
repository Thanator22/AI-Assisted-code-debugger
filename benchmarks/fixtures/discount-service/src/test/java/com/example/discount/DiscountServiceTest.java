package com.example.discount;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import org.junit.jupiter.api.Test;

class DiscountServiceTest {

    private final DiscountService service =
            new DiscountService();

    @Test
    void doesNotApplyDiscountBelowThreshold() {
        int total = service.calculateTotal(100, 9);

        assertEquals(900, total);
    }

    @Test
    void appliesDiscountAboveThreshold() {
        int total = service.calculateTotal(100, 11);

        assertEquals(990, total);
    }

    @Test
    void appliesDiscountAtThreshold() {
        int total = service.calculateTotal(100, 10);

        assertEquals(900, total);
    }

    @Test
    void rejectsNonPositiveQuantity() {
        assertThrows(
                IllegalArgumentException.class,
                () -> service.calculateTotal(100, 0)
        );
    }
}