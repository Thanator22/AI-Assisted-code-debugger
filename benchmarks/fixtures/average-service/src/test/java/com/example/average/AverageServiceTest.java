package com.example.average;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import org.junit.jupiter.api.Test;

class AverageServiceTest {

    private final AverageService service = new AverageService();

    @Test
    void preservesPositiveFraction() {
        assertEquals(
                1.5,
                service.average(new int[]{1, 2}),
                0.000001
        );
    }

    @Test
    void preservesNegativeFraction() {
        assertEquals(
                -1.5,
                service.average(new int[]{-1, -2}),
                0.000001
        );
    }

    @Test
    void calculatesWholeNumberAverage() {
        assertEquals(
                3.0,
                service.average(new int[]{2, 4}),
                0.000001
        );
    }

    @Test
    void handlesLargeValuesWithoutIntegerSumOverflow() {
        assertEquals(
                (double) Integer.MAX_VALUE,
                service.average(new int[]{
                        Integer.MAX_VALUE,
                        Integer.MAX_VALUE
                }),
                0.000001
        );
    }

    @Test
    void rejectsEmptyInput() {
        assertThrows(
                IllegalArgumentException.class,
                () -> service.average(new int[]{})
        );
    }

    @Test
    void rejectsNullInput() {
        assertThrows(
                IllegalArgumentException.class,
                () -> service.average(null)
        );
    }
}