/*
 * Modulino Movement - Basic
 * 
 * This example demonstrates how to read motion data from the Modulino Movement sensor.
 * This sensor contains an IMU (Inertial Measurement Unit) with both an accelerometer
 * and a gyroscope.
 * 
 * Sensor capabilities:
 * - Accelerometer: Measures linear acceleration in 3 axes (X, Y, Z)
 *   Values are in g-force (1g = 9.8 m/s²)
 *   Typical range: -2g to +2g
 * 
 * - Gyroscope: Measures rotational velocity around 3 axes (roll, pitch, yaw)
 *   Values are in degrees per second (°/s)
 *   Roll: Rotation around X-axis (tilting left/right)
 *   Pitch: Rotation around Y-axis (tilting forward/backward)
 *   Yaw: Rotation around Z-axis (spinning clockwise/counterclockwise)
 * 
 * Applications:
 * - Tilt detection
 * - Gesture recognition
 * - Motion tracking
 * - Orientation sensing
 * - Vibration monitoring
 *
 * This example code is in the public domain. 
 * Copyright (c) 2025 Arduino
 * SPDX-License-Identifier: MPL-2.0
 */

#include <Arduino_Modulino.h>

// Create a ModulinoMovement object
ModulinoMovement movement;


// Variables to store accelerometer readings (g-force)
float x, y, z;

/* Constant defines -------------------------------------------------------- */
#define CONVERT_G_TO_MS2 9.80665f
#define FREQUENCY_HZ 50
#define INTERVAL_MS (1000 / (FREQUENCY_HZ + 1))

static unsigned long last_interval_ms = 0;

void setup() {
  Serial.begin(9600);
  // Initialize Modulino I2C communication
  Modulino.begin();
  // Detect and connect to movement sensor module
  movement.begin();
}

void loop() {

  if (millis() > last_interval_ms + INTERVAL_MS) {
    last_interval_ms = millis();

    if (movement.available()) {
      // Read new movement data from the sensor
      // Call this before reading individual values
      movement.update();

      // Get acceleration values (in g-force, where 1g ≈ 9.8 m/s²)
      x = movement.getX();  // X-axis acceleration
      y = movement.getY();  // Y-axis acceleration
      z = movement.getZ();  // Z-axis acceleration (typically ~1.0 when upright due to gravity)

      // converting to m/s2
      float ax_m_s2 = x * CONVERT_G_TO_MS2;
      float ay_m_s2 = y * CONVERT_G_TO_MS2;
      float az_m_s2 = z * CONVERT_G_TO_MS2;

      Serial.print(ax_m_s2);
      Serial.print("\t");
      Serial.print(ay_m_s2);
      Serial.print("\t");
      Serial.println(az_m_s2);
    }
  }
}