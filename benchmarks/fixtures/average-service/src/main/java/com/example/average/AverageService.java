package com.example.average;

public class AverageService {

    public double average(int[] values) {
        if (values == null || values.length == 0) {
            throw new IllegalArgumentException("Values must not be empty");
        }

        long sum = 0;
        for (int value : values) {
            sum += value;
        }

        return sum / values.length;
    }
}
