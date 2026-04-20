import java.text.SimpleDateFormat
import java.util.Date

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

fun gitSha(): String = try {
    val p = ProcessBuilder("git", "rev-parse", "--short", "HEAD")
        .directory(rootDir.parentFile)
        .redirectErrorStream(true)
        .start()
    p.inputStream.bufferedReader().readText().trim().ifEmpty { "unknown" }
} catch (_: Exception) { "unknown" }

fun buildStamp(): String =
    SimpleDateFormat("yyyy-MM-dd HH:mm").format(Date())

android {
    namespace = "dev.robot.companion"
    compileSdk = 34

    defaultConfig {
        applicationId = "dev.robot.companion"
        minSdk = 26
        targetSdk = 34
        versionCode = 3
        versionName = "0.3.0"
        buildConfigField("String", "GIT_SHA", "\"${gitSha()}\"")
        buildConfigField("String", "BUILD_STAMP", "\"${buildStamp()}\"")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
        debug {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    packaging {
        resources {
            excludes += setOf(
                "META-INF/AL2.0",
                "META-INF/LGPL2.1",
                "META-INF/DEPENDENCIES",
                "META-INF/LICENSE",
                "META-INF/LICENSE.txt",
                "META-INF/NOTICE",
                "META-INF/NOTICE.txt",
            )
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.lifecycle:lifecycle-service:2.8.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // HTTP
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // CameraX
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")
    implementation("androidx.camera:camera-view:1.3.4")
}
