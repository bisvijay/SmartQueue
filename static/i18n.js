/* SmartQueue language picker: English / Hindi (हिंदी).
 *
 * The app renders English on the server. When the visitor picks Hindi, this
 * script translates visible UI text in place using the dictionary below and
 * keeps dynamically rendered content (queue rows, notices) translated via a
 * MutationObserver. The choice persists in localStorage. Business data such
 * as provider names, addresses, and service lists is never translated — only
 * exact UI strings and the parameterized patterns at the bottom.
 */
(() => {
  "use strict";

  const STORAGE_KEY = "smartqueue.lang";

  const HI = {
    // Header and navigation
    "Discover": "खोजें",
    "How it works": "यह कैसे काम करता है",
    "For providers": "प्रदाताओं के लिए",
    "My queue": "मेरी कतार",
    "My queues": "मेरी कतारें",
    "Sign out": "साइन आउट",
    "Sign up": "साइन अप",
    "Log in": "लॉग इन",
    "Sign in": "साइन इन",
    "Create account": "खाता बनाएँ",
    "Create an account": "खाता बनाएँ",
    "Customer discovery": "ग्राहक खोज",
    "SmartQueue home": "SmartQueue होम",
    "Administrator": "प्रशासक",
    "Admin": "एडमिन",
    "Administrator sign in": "प्रशासक साइन इन",

    // Hero
    "YOUR NEIGHBORHOOD, IN REAL TIME": "आपका मोहल्ला, रियल टाइम में",
    "Your time,": "आपका समय,",
    "better": "और बेहतर",
    "spent.": "बिताएँ।",
    "Find a spot in line before you leave. More living, less waiting.": "निकलने से पहले लाइन में अपनी जगह पक्की करें। ज़्यादा जीना, कम इंतज़ार।",
    "Find your place": "अपनी जगह खोजें",
    "YOUR SPOT IS SAVED": "आपकी जगह सुरक्षित है",
    "in line · about 12 min": "लाइन में · लगभग 12 मिनट",
    "A little more": "आपके दिन में",
    "room in your day.": "थोड़ी और जगह।",

    // Discover
    "THE GOOD STUFF, RIGHT AROUND YOU": "सबसे अच्छी चीज़ें, बिल्कुल आपके आस-पास",
    "Where to next?": "अगली मंज़िल कहाँ?",
    "Search places or services": "स्थान या सेवाएँ खोजें",
    "Try “coffee”, “haircut”, or a place name": "“कॉफ़ी”, “हेयरकट” या किसी जगह का नाम लिखें",
    "Your location": "आपका स्थान",
    "City, state, or ZIP": "शहर, राज्य या ZIP",
    "Show search results": "खोज परिणाम दिखाएँ",
    "Filter providers by category": "श्रेणी के अनुसार प्रदाता फ़िल्टर करें",
    "All services": "सभी सेवाएँ",
    "Salon": "सैलून",
    "Healthcare": "स्वास्थ्य सेवा",
    "Food & drink": "खाना-पीना",
    "Government": "सरकारी",
    "Other": "अन्य",
    "Favorites": "पसंदीदा",
    "Showing places near": "आस-पास के स्थान:",
    "all locations": "सभी स्थान",
    "On your radar": "आपकी नज़र में",
    "Good places, with a little less guesswork.": "अच्छी जगहें, बिना ज़्यादा अंदाज़े के।",
    "All nearby": "आस-पास के सभी",
    "Business contact": "व्यवसाय संपर्क",

    // Queue status and actions
    "YOU’RE BEING SERVED": "आपकी सेवा चल रही है",
    "YOU’RE ON THE LIST": "आप सूची में हैं",
    "You’re on the list.": "आप सूची में हैं।",
    "Your place": "आपकी जगह",
    "Estimated wait": "अनुमानित प्रतीक्षा",
    "estimated wait": "अनुमानित प्रतीक्षा",
    "min": "मिनट",
    "in line": "लाइन में",
    "We’ll keep your place here": "हम आपकी जगह यहाँ सुरक्षित रखेंगे",
    "Leave queue": "कतार छोड़ें",
    "Join queue": "कतार में शामिल हों",
    "What service do you need?": "आपको कौन-सी सेवा चाहिए?",
    "You can pick more than one": "आप एक से अधिक चुन सकते हैं",
    "You can pick more than one.": "आप एक से अधिक चुन सकते हैं।",
    "Cancel": "रद्द करें",
    "You’re in line ✓": "आप लाइन में हैं ✓",
    "Queue paused": "कतार रुकी है",
    "Log in to join": "शामिल होने के लिए लॉग इन करें",
    "Open": "खुला",
    "Paused": "रुका हुआ",
    "Now": "अभी",
    "Serving": "सेवा चल रही है",
    "Service in progress": "सेवा चल रही है",
    "Complete": "पूरा करें",
    "Completed": "पूरा हुआ",
    "COMPLETED": "पूरा हुआ",
    "Next up": "अगला नंबर",
    "Waiting": "प्रतीक्षा में",
    "Start service": "सेवा शुरू करें",
    "Customer": "ग्राहक",
    "Provider": "प्रदाता",
    "No places found just yet": "अभी कोई जगह नहीं मिली",
    "Try another search or choose a different category.": "कोई और खोज आज़माएँ या दूसरी श्रेणी चुनें।",
    "Clear filters": "फ़िल्टर हटाएँ",
    "Add providers to the data store to see them here.": "यहाँ देखने के लिए डेटा स्टोर में प्रदाता जोड़ें।",
    "Preview mode.": "पूर्वावलोकन मोड।",
    "Queue counts begin with sample data; queue actions are saved to this server’s JSON data store.": "कतार की संख्या नमूना डेटा से शुरू होती है; कतार की कार्रवाइयाँ इस सर्वर के JSON डेटा स्टोर में सहेजी जाती हैं।",

    // How it works
    "A BETTER KIND OF WAIT": "इंतज़ार का एक बेहतर तरीका",
    "Make room for": "अपने दिन के लिए",
    "your day.": "जगह बनाएँ।",
    "See nearby services and their current wait, all in one spot.": "आस-पास की सेवाएँ और उनका वर्तमान इंतज़ार, सब एक ही जगह देखें।",
    "Save your spot": "अपनी जगह सुरक्षित करें",
    "Join a virtual queue and get a clear picture of what’s ahead.": "वर्चुअल कतार में शामिल हों और आगे की स्थिति साफ़ देखें।",
    "Arrive when it’s right": "सही समय पर पहुँचें",
    "Spend less time standing around and more time doing your thing.": "खड़े रहकर इंतज़ार में कम, अपने काम में ज़्यादा समय बिताएँ।",

    // Footer
    "Make room for what matters.": "जो ज़रूरी है, उसके लिए जगह बनाएँ।",
    "Less waiting starts here.": "कम इंतज़ार यहीं से शुरू होता है।",
    "Explore": "देखें",
    "Find a service": "सेवा खोजें",
    "For business": "व्यवसाय के लिए",
    "Provider dashboard": "प्रदाता डैशबोर्ड",
    "Admin demo": "एडमिन डेमो",
    "Say hello": "संपर्क करें",
    "Contact us": "हमसे संपर्क करें",
    "Privacy & terms": "गोपनीयता और शर्तें",
    "© 2025 SmartQueue. Your time is yours.": "© 2025 SmartQueue। आपका समय, आपका है।",
    "Your time is yours.": "आपका समय, आपका है।",
    "Made for the in-between moments.": "बीच के पलों के लिए बनाया गया।",
    "← Back to SmartQueue": "← SmartQueue पर वापस जाएँ",

    // Provider dashboard
    "PROVIDER SPACE": "प्रदाता क्षेत्र",
    "Expand display": "डिस्प्ले बड़ा करें",
    "Collapse display": "डिस्प्ले छोटा करें",
    "Open for walk-ins": "वॉक-इन के लिए खुला है",
    "People in queue": "कतार में लोग",
    "Waiting or in service": "प्रतीक्षा में या सेवा में",
    "Based on your service time": "आपके सेवा समय के आधार पर",
    "Average service": "औसत सेवा समय",
    "Your current estimate": "आपका वर्तमान अनुमान",
    "Service status": "सेवा स्थिति",
    "Customer visibility": "ग्राहक दृश्यता",
    "RIGHT NOW": "अभी",
    "Filter queue services": "कतार सेवाएँ फ़िल्टर करें",
    "In queue": "कतार में",
    "Completed services": "पूरी हुई सेवाएँ",
    "CUSTOMER": "ग्राहक",
    "SERVICE": "सेवा",
    "Filter by service": "सेवा के अनुसार फ़िल्टर करें",
    "View options": "दृश्य विकल्प",
    "Contact": "संपर्क",
    "Wait": "प्रतीक्षा",
    "No customers for this service.": "इस सेवा के लिए कोई ग्राहक नहीं है।",
    "WAIT": "प्रतीक्षा",
    "STATUS / ACTION": "स्थिति / कार्रवाई",
    "No one waiting. Enjoy the breather.": "कोई प्रतीक्षा नहीं कर रहा। आराम कीजिए।",
    "No completed services yet.": "अभी कोई सेवा पूरी नहीं हुई।",
    "customer in your queue": "ग्राहक आपकी कतार में",
    "customers in your queue": "ग्राहक आपकी कतार में",
    "completed service": "सेवा पूरी हुई",
    "completed services": "सेवाएँ पूरी हुईं",
    "Start next": "अगला शुरू करें",
    "CUSTOMER ENTRY": "ग्राहक प्रवेश",
    "Join your queue": "आपकी कतार में शामिल हों",
    "Scan to find this listing and join the queue.": "इस लिस्टिंग को खोजने और कतार में शामिल होने के लिए स्कैन करें।",
    "Your listing QR code will be available after approval.": "अनुमोदन के बाद आपकी लिस्टिंग का QR कोड उपलब्ध होगा।",
    "YOUR PLACE": "आपकी जगह",
    "Business settings": "व्यवसाय सेटिंग",
    "Update the details customers see on your listing. Queue counts, ratings, and reviews are managed by SmartQueue.": "वे विवरण बदलें जो ग्राहक आपकी लिस्टिंग पर देखते हैं। कतार संख्या, रेटिंग और समीक्षाएँ SmartQueue द्वारा प्रबंधित होती हैं।",
    "No listing photo": "कोई लिस्टिंग फ़ोटो नहीं",
    "Business photo": "व्यवसाय फ़ोटो",
    "Profile photo": "प्रोफ़ाइल फ़ोटो",
    "JPEG, PNG, or WebP; under 5 MB": "JPEG, PNG या WebP; 5 MB से कम",
    "Update business photo": "व्यवसाय फ़ोटो बदलें",
    "Business name": "व्यवसाय का नाम",
    "Category": "श्रेणी",
    "Address": "पता",
    "Tagline": "टैगलाइन",
    "Short description shown on your public listing (160 characters max)": "आपकी सार्वजनिक लिस्टिंग पर दिखने वाला छोटा विवरण (अधिकतम 160 अक्षर)",
    "Business contact email": "व्यवसाय संपर्क ईमेल",
    "Business contact phone": "व्यवसाय संपर्क फ़ोन",
    "Indian numbers default to +91; use +country code otherwise": "भारतीय नंबरों के लिए डिफ़ॉल्ट +91; अन्यथा +देश कोड लिखें",
    "These details appear on your public business listing.": "ये विवरण आपकी सार्वजनिक व्यवसाय लिस्टिंग पर दिखते हैं।",
    "Services": "सेवाएँ",
    "Comma separated, up to 20 items (60 characters each)": "कॉमा से अलग करें, अधिकतम 20 आइटम (प्रत्येक 60 अक्षर)",
    "Opening hours": "खुलने का समय",
    "Average service time (minutes)": "औसत सेवा समय (मिनट)",
    "Time per service": "प्रत्येक सेवा का समय",
    "Minutes each service takes — used for wait estimates. Blank means the average above.": "प्रत्येक सेवा में लगने वाले मिनट — प्रतीक्षा अनुमान में उपयोग होते हैं। खाली छोड़ने पर ऊपर का औसत समय लागू होगा।",
    "Save settings": "सेटिंग सहेजें",
    "IN PERSON": "व्यक्तिगत रूप से",
    "Add walk-in": "वॉक-इन जोड़ें",
    "Customer name": "ग्राहक का नाम",
    "Email address": "ईमेल पता",
    "Enter an email, a phone, or both": "ईमेल, फ़ोन या दोनों लिखें",
    "Enter an email, a phone number, or both": "ईमेल, फ़ोन नंबर या दोनों लिखें",
    "Enter an email, a phone number, or both.": "ईमेल, फ़ोन नंबर या दोनों लिखें।",
    "Phone number": "फ़ोन नंबर",
    "Address (optional)": "पता (वैकल्पिक)",
    "Service": "सेवा",
    "General service": "सामान्य सेवा",
    "Add to queue": "कतार में जोड़ें",
    "Walk-ins can be added after your listing is approved.": "आपकी लिस्टिंग स्वीकृत होने के बाद वॉक-इन जोड़े जा सकते हैं।",
    "Queue and business settings are saved to this server’s local data store.": "कतार और व्यवसाय सेटिंग इस सर्वर के स्थानीय डेटा स्टोर में सहेजी जाती हैं।",
    "Listing pending review": "लिस्टिंग समीक्षा लंबित है",
    "Listing approved": "लिस्टिंग स्वीकृत",
    "Listing rejected": "लिस्टिंग अस्वीकृत",
    "Your listing is hidden from customers until an administrator approves it.": "जब तक प्रशासक स्वीकृति नहीं देता, आपकी लिस्टिंग ग्राहकों से छिपी रहती है।",
    "Update your listing details, then request another review.": "अपनी लिस्टिंग के विवरण बदलें, फिर दोबारा समीक्षा का अनुरोध करें।",
    "Request another review": "दोबारा समीक्षा का अनुरोध करें",
    "Your listing must be approved first": "पहले आपकी लिस्टिंग स्वीकृत होनी चाहिए",
    "No provider records found.": "कोई प्रदाता रिकॉर्ड नहीं मिला।",

    // Admin approvals
    "SMARTQUEUE · ADMIN": "SMARTQUEUE · एडमिन",
    "Provider approvals": "प्रदाता स्वीकृतियाँ",
    "Review provider listings before they appear in customer discovery.": "ग्राहक खोज में दिखने से पहले प्रदाता लिस्टिंग की समीक्षा करें।",
    "Private admin area": "निजी एडमिन क्षेत्र",
    "Pending review": "समीक्षा लंबित",
    "Awaiting your decision": "आपके निर्णय की प्रतीक्षा है",
    "Approved providers": "स्वीकृत प्रदाता",
    "Visible in discovery": "खोज में दिखाई देते हैं",
    "Total providers": "कुल प्रदाता",
    "All listings": "सभी लिस्टिंग",
    "Avg. wait time": "औसत प्रतीक्षा समय",
    "Across approved providers": "स्वीकृत प्रदाताओं में",
    "MODERATION QUEUE": "मॉडरेशन कतार",
    "Provider listings": "प्रदाता लिस्टिंग",
    "No tagline provided.": "कोई टैगलाइन नहीं दी गई।",
    "No business email": "कोई व्यवसाय ईमेल नहीं",
    "None listed": "कोई सूचीबद्ध नहीं",
    "Review note": "समीक्षा नोट",
    "Optional reason or guidance": "वैकल्पिक कारण या मार्गदर्शन",
    "Approve": "स्वीकृत करें",
    "Reject": "अस्वीकार करें",
    "Pending": "लंबित",
    "Approved": "स्वीकृत",
    "Rejected": "अस्वीकृत",
    "No provider listings to review.": "समीक्षा के लिए कोई प्रदाता लिस्टिंग नहीं है।",

    // Log in
    "WELCOME BACK": "फिर से स्वागत है",
    "Good to see": "आपको फिर से",
    "you again.": "देखकर खुशी हुई।",
    "Sign in to pick up where you left off.": "जहाँ छोड़ा था, वहीं से शुरू करने के लिए साइन इन करें।",
    "Email address or phone number": "ईमेल पता या फ़ोन नंबर",
    "Password": "पासवर्ड",
    "Reset password": "पासवर्ड रीसेट करें",
    "New to SmartQueue?": "SmartQueue पर नए हैं?",

    // Registration
    "WELCOME TO SMARTQUEUE": "SMARTQUEUE में आपका स्वागत है",
    "Make room for": "अपने दिन के लिए",
    "Create an account to find your place in line or bring your business to SmartQueue.": "लाइन में अपनी जगह पाने या अपने व्यवसाय को SmartQueue पर लाने के लिए खाता बनाएँ।",
    "Your account is ready. Sign in to manage your business or start finding your place in line.": "आपका खाता तैयार है। अपना व्यवसाय प्रबंधित करने या लाइन में अपनी जगह खोजने के लिए साइन इन करें।",
    "Registration complete": "पंजीकरण पूरा हुआ",
    "Your account has been saved. You can now sign in.": "आपका खाता सहेजा गया है। अब आप साइन इन कर सकते हैं।",
    "Log in to SmartQueue": "SmartQueue में लॉग इन करें",
    "Please review your details.": "कृपया अपने विवरण जांचें।",
    "I’m joining as": "मैं इस रूप में शामिल हो रहा हूँ",
    "Find and join queues": "कतारें खोजें और शामिल हों",
    "Service provider": "सेवा प्रदाता",
    "Add your business listing": "अपनी व्यवसाय लिस्टिंग जोड़ें",
    "Your name": "आपका नाम",
    "(optional)": "(वैकल्पिक)",
    "(India is the default country)": "(भारत डिफ़ॉल्ट देश है)",
    "Confirm password": "पासवर्ड की पुष्टि करें",
    "At least 10 characters": "कम से कम 10 अक्षर",
    "Use at least 10 characters.": "कम से कम 10 अक्षरों का उपयोग करें।",
    "JPEG, PNG, or WebP; up to 5 MB. Customer photos are private; provider photos appear on your business card.": "JPEG, PNG या WebP; अधिकतम 5 MB। ग्राहक फ़ोटो निजी रहती हैं; प्रदाता फ़ोटो आपके व्यवसाय कार्ड पर दिखती हैं।",
    "Your selected photo is saved and will be used when you submit this form.": "आपकी चुनी हुई फ़ोटो सहेजी गई है और फ़ॉर्म जमा करने पर उपयोग होगी।",
    "YOUR BUSINESS": "आपका व्यवसाय",
    "Tell customers a little about the place they’ll visit.": "ग्राहकों को उस जगह के बारे में थोड़ा बताएँ जहाँ वे आएँगे।",
    "Business category": "व्यवसाय श्रेणी",
    "Choose a category": "श्रेणी चुनें",
    "Business address": "व्यवसाय का पता",
    "(optional, comma-separated)": "(वैकल्पिक, कॉमा से अलग)",
    "By creating an account, you agree to use this local SmartQueue demo. Passwords are stored as salted hashes.": "खाता बनाकर, आप इस स्थानीय SmartQueue डेमो का उपयोग करने के लिए सहमत होते हैं। पासवर्ड सॉल्टेड हैश के रूप में संग्रहीत होते हैं।",

    // Password reset
    "ACCOUNT RECOVERY": "खाता पुनर्प्राप्ति",
    "Check your": "अपना खाता",
    "account.": "देखें।",
    "If those contact details match an account, its password has been updated. You can try logging in now.": "यदि वे संपर्क विवरण किसी खाते से मेल खाते हैं, तो उसका पासवर्ड बदल दिया गया है। अब आप लॉग इन करके देख सकते हैं।",
    "This local demo checks both registered email and phone. Contact details are not verified; use a trusted environment only.": "यह स्थानीय डेमो पंजीकृत ईमेल और फ़ोन दोनों की जांच करता है। संपर्क विवरण सत्यापित नहीं होते; केवल विश्वसनीय वातावरण में उपयोग करें।",
    "Return to log in": "लॉग इन पर वापस जाएँ",
    "Reset your": "अपना पासवर्ड",
    "password.": "रीसेट करें।",
    "Enter the email and phone number on your account, then choose a new password.": "अपने खाते का ईमेल और फ़ोन नंबर लिखें, फिर नया पासवर्ड चुनें।",
    "Account email": "खाते का ईमेल",
    "Account phone number": "खाते का फ़ोन नंबर",
    "New password": "नया पासवर्ड",
    "Confirm new password": "नए पासवर्ड की पुष्टि करें",
    "Back to log in": "लॉग इन पर वापस",

    // Public walk-in page
    "JOIN THE QUEUE": "कतार में शामिल हों",
    "Add your details to join the queue. Your contact information is shared only with this provider.": "कतार में शामिल होने के लिए अपने विवरण जोड़ें। आपकी संपर्क जानकारी केवल इस प्रदाता के साथ साझा की जाती है।",
    "This provider is not accepting customers right now.": "यह प्रदाता अभी ग्राहक नहीं ले रहा है।",
    "Full name": "पूरा नाम",

    // Administrator sign in
    "SMARTQUEUE ADMIN": "SMARTQUEUE एडमिन",
    "Review provider": "प्रदाता लिस्टिंग की",
    "listings.": "समीक्षा करें।",
    "Sign in to approve or reject businesses before they appear in customer discovery.": "ग्राहक खोज में दिखने से पहले व्यवसायों को स्वीकृत या अस्वीकृत करने के लिए साइन इन करें।",
    "Administrator password": "प्रशासक पासवर्ड",
    "Provider listings require approval.": "प्रदाता लिस्टिंग के लिए स्वीकृति आवश्यक है।",

    // Page titles
    "SmartQueue — Your time, better spent": "SmartQueue — आपका समय, बेहतर उपयोग",
    "Log in — SmartQueue": "लॉग इन — SmartQueue",
    "Create your account — SmartQueue": "अपना खाता बनाएँ — SmartQueue",
    "Reset password — SmartQueue": "पासवर्ड रीसेट करें — SmartQueue",
    "Administrator sign in — SmartQueue": "प्रशासक साइन इन — SmartQueue",
  };

  // Parameterized strings: [pattern, replacement] applied to the trimmed text.
  const PATTERNS = [
    [/^~(\d+) min$/, "~$1 मिनट"],
    [/^(\d+) min$/, "$1 मिनट"],
    [/^(\d+) people$/, "$1 लोग"],
    [/^Queue snapshots · (.+)$/, "कतार की झलक · $1"],
    [/^Here’s how things are moving at (.+)\.$/, "$1 में गतिविधियाँ ऐसे चल रही हैं।"],
    [/^Welcome to (.+)$/, "$1 में आपका स्वागत है"],
    [/^Your place at (.+) is saved\.$/, "$1 पर आपकी जगह सुरक्षित है।"],
    [/^Remove (.+) from favorites$/, "$1 को पसंदीदा से हटाएँ"],
    [/^Add (.+) to favorites$/, "$1 को पसंदीदा में जोड़ें"],
    [/^QR code to discover (.+) and join its queue$/, "$1 को खोजने और उसकी कतार में शामिल होने का QR कोड"],
    [/^Current photo for (.+)$/, "$1 की वर्तमान फ़ोटो"],
    [/^Services: (.+) · Hours: (.+)$/, "सेवाएँ: $1 · समय: $2"],
    [/^Review note: (.+)$/, "समीक्षा नोट: $1"],
    [/^Joined (.+)$/, "शामिल हुए: $1"],
    [/^Join (.+)’s queue — SmartQueue$/, "$1 की कतार में शामिल हों — SmartQueue"],
    [/^Join (.+)’s queue\.$/, "$1 की कतार में शामिल हों।"],
    [/^Nothing picked\? We’ll book “(.+)”\.$/, "कुछ नहीं चुना? हम “$1” बुक करेंगे।"],
    [/^Service: (.+)$/, "सेवा: $1"],
    [/^If you pick none, (.+) will be booked\.$/, "यदि आप कुछ नहीं चुनते, तो $1 बुक होगी।"],
  ];

  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA"]);
  const ATTRS = ["placeholder", "title", "aria-label"];

  let current = "en";
  try {
    current = window.localStorage.getItem(STORAGE_KEY) === "hi" ? "hi" : "en";
  } catch (error) {
    current = "en";
  }

  const textOriginals = new Map();
  const attrOriginals = new Map();
  let titleOriginal = null;
  let applying = false;

  function translateString(text) {
    const trimmed = text.trim();
    if (!trimmed) return null;
    let translated = null;
    if (Object.prototype.hasOwnProperty.call(HI, trimmed)) {
      translated = HI[trimmed];
    } else {
      for (const [pattern, replacement] of PATTERNS) {
        if (pattern.test(trimmed)) {
          translated = trimmed.replace(pattern, replacement);
          break;
        }
      }
    }
    if (translated === null || translated === trimmed) return null;
    const start = text.indexOf(trimmed);
    return text.slice(0, start) + translated + text.slice(start + trimmed.length);
  }

  function isSkippable(node) {
    let element = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
    while (element) {
      if (SKIP_TAGS.has(element.tagName)) return true;
      if (element.classList && element.classList.contains("language-picker-wrap")) return true;
      if (element.hasAttribute && (element.hasAttribute("data-no-i18n") || element.getAttribute("translate") === "no")) return true;
      element = element.parentElement;
    }
    return false;
  }

  function translateTextNode(node) {
    if (!node.nodeValue || isSkippable(node)) return;
    const translated = translateString(node.nodeValue);
    if (translated === null) return;
    if (!textOriginals.has(node)) textOriginals.set(node, node.nodeValue);
    node.nodeValue = translated;
  }

  function translateAttributes(element) {
    if (isSkippable(element)) return;
    for (const attr of ATTRS) {
      const value = element.getAttribute && element.getAttribute(attr);
      if (!value) continue;
      const translated = translateString(value);
      if (translated === null) continue;
      if (!attrOriginals.has(element)) attrOriginals.set(element, {});
      const saved = attrOriginals.get(element);
      if (!(attr in saved)) saved[attr] = value;
      element.setAttribute(attr, translated);
    }
  }

  function translateRoot(root) {
    if (!root) return;
    if (root.nodeType === Node.TEXT_NODE) {
      translateTextNode(root);
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE && root !== document) return;
    const scope = root === document ? document.body : root;
    if (!scope) return;
    if (scope.nodeType === Node.ELEMENT_NODE) translateAttributes(scope);
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) translateTextNode(node);
    scope.querySelectorAll("[placeholder], [title], [aria-label]").forEach(translateAttributes);
  }

  function restoreOriginals() {
    for (const [node, value] of textOriginals) {
      if (node.isConnected) node.nodeValue = value;
    }
    textOriginals.clear();
    for (const [element, saved] of attrOriginals) {
      if (!element.isConnected) continue;
      for (const [attr, value] of Object.entries(saved)) element.setAttribute(attr, value);
    }
    attrOriginals.clear();
    if (titleOriginal !== null) {
      document.title = titleOriginal;
      titleOriginal = null;
    }
  }

  function applyLanguage(language) {
    current = language === "hi" ? "hi" : "en";
    try {
      window.localStorage.setItem(STORAGE_KEY, current);
    } catch (error) {
      /* storage unavailable; the picker still works for this page view */
    }
    document.documentElement.lang = current;
    const picker = document.querySelector(".language-picker");
    if (picker) picker.value = current;
    applying = true;
    try {
      if (current === "hi") {
        if (titleOriginal === null) titleOriginal = document.title;
        const translatedTitle = translateString(document.title);
        if (translatedTitle !== null) document.title = translatedTitle;
        translateRoot(document);
      } else {
        restoreOriginals();
      }
    } finally {
      applying = false;
    }
  }

  function injectPicker() {
    if (document.querySelector(".language-picker")) return;
    const host = document.querySelector(".header-actions") || document.querySelector(".header-inner");
    if (!host) return;
    const wrap = document.createElement("span");
    wrap.className = "language-picker-wrap";
    const select = document.createElement("select");
    select.className = "language-picker";
    select.setAttribute("aria-label", "Language / भाषा चुनें");
    const english = document.createElement("option");
    english.value = "en";
    english.textContent = "English";
    const hindi = document.createElement("option");
    hindi.value = "hi";
    hindi.textContent = "हिंदी (Hindi)";
    select.append(english, hindi);
    select.value = current;
    select.addEventListener("change", () => applyLanguage(select.value));
    wrap.append(select);
    host.append(wrap);
  }

  const observer = new MutationObserver((mutations) => {
    if (current !== "hi" || applying) return;
    applying = true;
    try {
      for (const mutation of mutations) {
        if (mutation.type === "characterData") {
          translateTextNode(mutation.target);
        } else {
          mutation.addedNodes.forEach((node) => translateRoot(node));
        }
      }
    } finally {
      applying = false;
    }
  });

  injectPicker();
  if (document.body) {
    observer.observe(document.body, { subtree: true, childList: true, characterData: true });
  }
  if (current === "hi") applyLanguage("hi");

  window.smartQueueI18n = {
    setLanguage: applyLanguage,
    getLanguage: () => current,
  };
})();
