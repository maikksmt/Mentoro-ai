// static/klaro/config.js

var klaroConfig = {
    version: 1,

    elementID: 'klaro',

    styling: {
        theme: ['light', 'bottom', 'wide'],
    },

    // Consent-Logik
    default: false,
    mustConsent: true,
    acceptAll: true,
    hideDeclineAll: false,
    hideLearnMore: false,
    noticeAsModal: false,

    storageMethod: 'cookie',
    cookieName: 'klaro',
    cookieExpiresAfterDays: 180,

    htmlTexts: true,

    // purposes: ['necessary', 'preferences', 'statistics', 'marketing'],

    translations: {
        zz: {
            privacyPolicyUrl: '/legal/privacy/',
        },

        de: {
            privacyPolicyUrl: '/de/legal/privacy/',
            privacyPolicy: {
                name: 'Datenschutzerklärung',
                text: 'Mehr Informationen findest du in unserer Datenschutzerklärung',
                // text: 'Mehr Informationen findest du in unserer {privacyPolicy}.',
            },
            consentModal: {
                title: 'Datenschutzeinstellungen',
                description:
                    'Hier kannst du auswählen, welche Arten von Cookies und Diensten du zulassen möchtest. Du kannst deine Auswahl später jederzeit ändern.',
            },
            consentNotice: {
                title: 'Wir respektieren deine Privatsphäre',
                description:
                    'Wir verwenden Cookies für grundlegende Funktionen, Statistik und optionale Dienste. Du kannst deine Auswahl jederzeit anpassen.',
                learnMore: 'Einstellungen öffnen',
            },
            purposes: {
                necessary: 'Notwendig',
                preferences: 'Präferenzen',
                statistics: 'Statistik',
                marketing: 'Marketing',
            },
            ok: 'Alle akzeptieren',
            acceptSelected: 'Auswahl speichern',
            decline: 'Nur notwendige',
        },

        en: {
            privacyPolicyUrl: '/en/legal/privacy/',
            privacyPolicy: {
                name: 'privacy policy',
                text: 'For more information, please see our privacy policy',
                // text: 'For more information, please see our {privacyPolicy}.',
            },
            consentModal: {
                title: 'Privacy settings',
                description:
                    'Here you can choose which types of cookies and services you want to allow. You can change your selection at any time.',
            },
            consentNotice: {
                title: 'We value your privacy',
                description:
                    'We use cookies for essential functions, statistics and optional services. You can change your preferences at any time.',
                learnMore: 'Open settings',
            },
            purposes: {
                necessary: 'Necessary',
                preferences: 'Preferences',
                statistics: 'Statistics',
                marketing: 'Marketing',
            },
            ok: 'Accept all',
            acceptSelected: 'Save selection',
            decline: 'Only necessary',
        },
    },


    services: [
        {
            name: 'essential-cookies',
            required: true,
            onlyOnce: true,
            purposes: ['necessary'],
            cookies: [
                'csrftoken',
                'sessionid',
                'htmx-current-path-for-history',
            ],
            translations: {
                zz: {
                    title: 'Essential cookies',
                },
                de: {
                    title: 'Technisch notwendige Cookies',
                    description:
                        'Diese Cookies sind für den Betrieb und die Sicherheit der Website erforderlich.',
                },
                en: {
                    title: 'Essential cookies',
                    description:
                        'These cookies are required for the basic operation and security of the website.',
                },
            },
        },

        {
            name: 'ui-preferences',
            onlyOnce: true,
            purposes: ['preferences'],
            cookies: [
                'preferred_theme',
                'django_language',
            ],
            translations: {
                zz: {
                    title: 'UI Preferences',
                },
                de: {
                    title: 'Benutzerpräferenzen (Theme & Sprache)',
                    description:
                        'Speichert deine Einstellungen für Design (Hell/Dunkel) und Sprache.',
                },
                en: {
                    title: 'User preferences (theme & language)',
                    description:
                        'Stores your choices for theme (light/dark) and language.',
                },
            },
        },

        {
            name: 'google-analytics',
            onlyOnce: true,
            purposes: ['statistics'],
            translations: {
                zz: {
                    title: 'Google Analytics',
                },
                de: {
                    title: 'Google Analytics',
                    description:
                        'Hilft uns zu verstehen, wie Besucher unsere Website nutzen. Deine IP-Adresse wird anonymisiert.',
                },
                en: {
                    title: 'Google Analytics',
                    description:
                        'Helps us understand how visitors use our site. Your IP address is anonymized.',
                },
            },

            onAccept: function () {
                gtag('consent', 'update', {
                    analytics_storage: 'granted',
                    ad_storage: 'denied',
                    ad_user_data: 'denied',
                    ad_personalization: 'denied',
                    functionality_storage: 'granted',
                    personalization_storage: 'denied',
                    security_storage: 'granted',
                });
                // wird bereits in base.html gesendet
                // gtag('config', 'G-ECWRCZVFD1', {anonymize_ip: true});
            },

            onDecline: function () {
                gtag('consent', 'update', {
                    analytics_storage: 'denied',
                    ad_storage: 'denied',
                    ad_user_data: 'denied',
                    ad_personalization: 'denied',
                    functionality_storage: 'denied',
                    personalization_storage: 'denied',
                    security_storage: 'granted',
                });
            },

            cookies: [/^_ga(_.+)?$/, /^_gid$/, /^_gat(_.+)?$/],
        },
    ]

};
