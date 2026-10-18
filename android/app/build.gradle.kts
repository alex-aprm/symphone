plugins {
    id("com.android.application")
}

android {
    namespace = "dev.phonesync"
    compileSdk = 36

    defaultConfig {
        applicationId = "dev.phonesync"
        minSdk = 30
        targetSdk = 36
        versionCode = 20
        versionName = "2.9.0"
    }

    // Release signing: CI passes the key through environment variables (GitHub secrets); local builds use the
    // debug key. Both must be the same key, or phones can't update the installed app.
    val ks = System.getenv("SYMPHONE_KEYSTORE")?.let { file(it) }?.takeIf { it.exists() }
    signingConfigs {
        if (ks != null) create("release") {
            storeFile = ks
            storePassword = System.getenv("SYMPHONE_KEYSTORE_PASSWORD")
            keyAlias = System.getenv("SYMPHONE_KEY_ALIAS")
            keyPassword = System.getenv("SYMPHONE_KEY_PASSWORD")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    // QR decoding for the in-app pairing scanner (core only, no Android UI library).
    implementation("com.google.zxing:core:3.5.3")
}
