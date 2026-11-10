/* Edge Impulse ingestion SDK
 * Copyright (c) 2022 EdgeImpulse Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

/* Includes ---------------------------------------------------------------- */
#include <Tool_motion_classification_-_UNO_Q_inferencing.h>
#include <Arduino_Modulino.h>

// Create a ModulinoMovement object
ModulinoMovement movement;

/* Constant defines -------------------------------------------------------- */
#define CONVERT_G_TO_MS2 9.80665f
#define MAX_ACCEPTED_RANGE 2.0f

/* Private variables ------------------------------------------------------- */
static bool debug_nn = false; 
static uint32_t run_inference_every_ms = 200;
static unsigned long last_inference_time = 0;
static float buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE] = { 0 };
static float inference_buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE];

// Smoothing structure for classification results
static ei_classifier_smooth_t smooth;

/**
 * @brief Return the sign of the number
 */
float ei_get_sign(float number) {
    return (number >= 0.0) ? 1.0 : -1.0;
}

void setup() {
    Serial.begin(115200);
    while (!Serial);
    Serial.println("Edge Impulse Inferencing Demo - Modulino Movement");

    // Initialize Modulino I2C communication and movement sensor
    Modulino.begin();
    if (!movement.begin()) {
        ei_printf("Failed to initialize Modulino Movement sensor!\r\n");
    } else {
        ei_printf("Modulino Movement sensor initialized\r\n");
    }

    if (EI_CLASSIFIER_RAW_SAMPLES_PER_FRAME != 3) {
        ei_printf("ERR: EI_CLASSIFIER_RAW_SAMPLES_PER_FRAME should be equal to 3 (the 3 sensor axes)\n");
        return;
    }

    // Initialize the smoother
    ei_classifier_smooth_init(&smooth, 10, 7, 0.8, 0.3);

    // Give time to fill the initial buffer
    delay((EI_CLASSIFIER_INTERVAL_MS * EI_CLASSIFIER_RAW_SAMPLE_COUNT) + 100);
}

void loop() {
    // Determine the next tick for precise sampling interval
    uint64_t next_tick = micros() + (EI_CLASSIFIER_INTERVAL_MS * 1000);

    // Roll the buffer -3 points to make room for the latest axis readings
    numpy::roll(buffer, EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE, -3);

    // Fetch new acceleration data from Modulino Movement
    movement.update();
    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 3] = movement.getX();
    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 2] = movement.getY();
    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 1] = movement.getZ();

    // Clamp values to accepted range
    for (int i = 0; i < 3; i++) {
        if (fabs(buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 3 + i]) > MAX_ACCEPTED_RANGE) {
            buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 3 + i] = ei_get_sign(buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 3 + i]) * MAX_ACCEPTED_RANGE;
        }
    }

    // Convert g-force to m/s^2
    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 3] *= CONVERT_G_TO_MS2;
    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 2] *= CONVERT_G_TO_MS2;
    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 1] *= CONVERT_G_TO_MS2;

    // Run inference periodically based on run_inference_every_ms without blocking sampling
    if (millis() - last_inference_time >= run_inference_every_ms) {
        last_inference_time = millis();

        // Copy current buffer for inference
        memcpy(inference_buffer, buffer, EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE * sizeof(float));

        signal_t signal;
        int err = numpy::signal_from_buffer(inference_buffer, EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE, &signal);
        if (err == 0) {
            ei_impulse_result_t result = { 0 };
            err = run_classifier(&signal, &result, debug_nn);
            if (err == EI_IMPULSE_OK) {
                ei_printf("Predictions (DSP: %d ms., Classification: %d ms.): ",
                    result.timing.dsp, result.timing.classification);
                
                const char *prediction = ei_classifier_smooth_update(&smooth, &result);
                ei_printf("%s\n", prediction);
            }
        }
    }

    // Wait for the next sampling tick
    uint64_t time_to_wait = next_tick - micros();
    if (time_to_wait > 0 && time_to_wait < 1000000) {
        delay((int)floor((float)time_to_wait / 1000.0f));
        delayMicroseconds(time_to_wait % 1000);
    }
}

#if !defined(EI_CLASSIFIER_SENSOR) || EI_CLASSIFIER_SENSOR != EI_CLASSIFIER_SENSOR_ACCELEROMETER
#error "Invalid model for current sensor"
#endif