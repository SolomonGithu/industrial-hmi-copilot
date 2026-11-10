/*
 * Industrial HMI Copilot
 *
 * MCU side:
 *   Modulino Movement -> Edge Impulse motion classifier -> RouterBridge
 */

#include <Arduino_RouterBridge.h>
#include <Arduino_Modulino.h>
#include <Tool_motion_classification_-_UNO_Q_inferencing.h>

ModulinoMovement movement;

#define CONVERT_G_TO_MS2 9.80665f
#define MAX_ACCEPTED_RANGE 2.0f

static bool debug_nn = false;
static uint32_t run_inference_every_ms = 200;
static unsigned long last_inference_time = 0;

static float buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE] = { 0 };
static float inference_buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE];
static ei_classifier_smooth_t smooth;


float ei_get_sign(float number) {
    return (number >= 0.0f) ? 1.0f : -1.0f;
}

/*
 * Map the current model names to the semantic names used by the
 * assembly workflow. In case you are using different labels!
 */
const char* normalize_class(const char* raw_class) {
    if (strcmp(raw_class, "tightening") == 0) {
        return "tightening";
    }

    if (strcmp(raw_class, "moving") == 0) {
        return "moving";
    }

    if (strcmp(raw_class, "idle") == 0) {
        return "idle";
    }

    // Future model names can pass through unchanged.
    return raw_class;
}

void setup() {
    Serial.begin(115200);

    Modulino.begin();

    if (!movement.begin()) {
        Serial.println("Failed to initialize Modulino Movement sensor.");
    } else {
        Serial.println("Modulino Movement sensor initialized.");
    }

    if (!Bridge.begin()) {
        Serial.println("Failed to initialize RouterBridge.");
    }

    if (EI_CLASSIFIER_RAW_SAMPLES_PER_FRAME != 3) {
        Serial.println("ERR: EI_CLASSIFIER_RAW_SAMPLES_PER_FRAME must be 3.");
        return;
    }

    // Same smoothing strategy as the original working classifier.
    ei_classifier_smooth_init(&smooth, 10, 7, 0.8, 0.3);

    delay((EI_CLASSIFIER_INTERVAL_MS * EI_CLASSIFIER_RAW_SAMPLE_COUNT) + 100);

    Serial.println("Assembly motion classifier ready.");
}

void send_motion_event(const char* raw_class, const char* normalized_class) {
    /*
     * MCU -> Linux:
     * Bridge.notify() is fire-and-forget. The Python side registers
     * Bridge.provide("motion_classification", callback).
     *
     * We send the smoothed classifier result at the inference rate.
     * The Python workflow layer de-duplicates repeated labels and only
     * advances the assembly state machine on meaningful transitions.
     *
     * Parameters are MessagePack-compatible:
     *   raw class, normalized class, inference timestamp
     */
    Bridge.notify(
        "motion_classification",
        String(raw_class),
        String(normalized_class),
        (uint32_t)millis()
    );

    Serial.print("Motion transition: ");
    Serial.print(raw_class);
    Serial.print(" -> ");
    Serial.println(normalized_class);
}

void loop() {
    uint64_t next_tick = micros() + (EI_CLASSIFIER_INTERVAL_MS * 1000);

    numpy::roll(
        buffer,
        EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE,
        -3
    );

    movement.update();

    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 3] = movement.getX();
    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 2] = movement.getY();
    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 1] = movement.getZ();

    for (int i = 0; i < 3; i++) {
        if (fabs(buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 3 + i]) > MAX_ACCEPTED_RANGE) {
            buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 3 + i] =
                ei_get_sign(
                    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 3 + i]
                ) * MAX_ACCEPTED_RANGE;
        }
    }

    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 3] *= CONVERT_G_TO_MS2;
    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 2] *= CONVERT_G_TO_MS2;
    buffer[EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE - 1] *= CONVERT_G_TO_MS2;

    if (millis() - last_inference_time >= run_inference_every_ms) {
        last_inference_time = millis();

        memcpy(
            inference_buffer,
            buffer,
            EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE * sizeof(float)
        );

        signal_t signal;

        int err = numpy::signal_from_buffer(
            inference_buffer,
            EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE,
            &signal
        );

        if (err == 0) {
            ei_impulse_result_t result = { 0 };

            err = run_classifier(
                &signal,
                &result,
                debug_nn
            );

            if (err == EI_IMPULSE_OK) {
                const char* prediction =
                    ei_classifier_smooth_update(&smooth, &result);

                if (prediction != nullptr && prediction[0] != '\0') {
                    const char* normalized =
                        normalize_class(prediction);

                    send_motion_event(
                        prediction,
                        normalized
                    );
                }
            }
        }
    }

    uint64_t time_to_wait = next_tick - micros();

    if (time_to_wait > 0 && time_to_wait < 1000000) {
        delay((int)floor((float)time_to_wait / 1000.0f));
        delayMicroseconds(time_to_wait % 1000);
    }
}

#if !defined(EI_CLASSIFIER_SENSOR) || EI_CLASSIFIER_SENSOR != EI_CLASSIFIER_SENSOR_ACCELEROMETER
#error "Invalid model for current sensor"
#endif
