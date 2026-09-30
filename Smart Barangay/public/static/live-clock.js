(() => {
    const timeZone = 'Asia/Manila';
    const dateFormatter = new Intl.DateTimeFormat('en-PH', {
        timeZone,
        weekday: 'short',
        month: 'short',
        day: 'numeric',
        year: 'numeric',
    });
    const timeFormatter = new Intl.DateTimeFormat('en-PH', {
        timeZone,
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit',
        hour12: true,
    });
    const dateTimeFormatter = new Intl.DateTimeFormat('en-PH', {
        timeZone,
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        hour12: true,
    });
    const syncDateFormatter = new Intl.DateTimeFormat('en-PH', {
        timeZone,
        month: 'short',
        day: 'numeric',
        year: 'numeric',
    });
    const monthYearFormatter = new Intl.DateTimeFormat('en-PH', {
        timeZone,
        month: 'short',
        year: 'numeric',
    });
    const monthFormatter = new Intl.DateTimeFormat('en-PH', {
        timeZone,
        month: 'short',
    });
    const monthPartsFormatter = new Intl.DateTimeFormat('en-CA', {
        timeZone,
        year: 'numeric',
        month: '2-digit',
    });

    const parseValue = (value) => {
        const raw = String(value ?? '').trim();
        if (!raw) return null;
        const parsed = /^\d{4}-\d{2}-\d{2}$/.test(raw)
            ? new Date(`${raw}T00:00:00+08:00`)
            : new Date(raw);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    };

    const formatDate = (value) => {
        const parsed = parseValue(value);
        return parsed ? dateFormatter.format(parsed) : String(value ?? '');
    };

    const formatDateTime = (value) => {
        const raw = String(value ?? '').trim();
        if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return formatDate(raw);
        const parsed = parseValue(raw);
        return parsed ? dateTimeFormatter.format(parsed) : raw;
    };

    const formatCurrentMonthRange = (now = new Date()) => {
        const [year, month] = monthPartsFormatter.format(now).split('-').map(Number);
        const start = new Date(Date.UTC(year, month - 1 - 6, 1));
        return `${monthYearFormatter.format(start)} - ${monthYearFormatter.format(now)}`;
    };

    const formatCurrentMonthLabel = (now = new Date()) => monthFormatter.format(now).toUpperCase();
    const formatLiveSync = (now = new Date()) => `${syncDateFormatter.format(now)} · ${timeFormatter.format(now)}`;

    const startLiveClock = ({ dateSelector, timeSelector, syncSelector, monthSelector } = {}) => {
        const dateElement = dateSelector ? document.querySelector(dateSelector) : null;
        const timeElement = timeSelector ? document.querySelector(timeSelector) : null;
        const syncElement = syncSelector ? document.querySelector(syncSelector) : null;
        const monthElement = monthSelector ? document.querySelector(monthSelector) : null;
        if (!dateElement && !timeElement && !syncElement && !monthElement) return () => {};

        const update = () => {
            const now = new Date();
            if (dateElement) dateElement.textContent = dateFormatter.format(now);
            if (timeElement) timeElement.textContent = timeFormatter.format(now);
            if (syncElement) syncElement.textContent = formatLiveSync(now);
            if (monthElement) monthElement.textContent = formatCurrentMonthLabel(now);
        };

        update();
        const timer = window.setInterval(update, 1000);
        return () => window.clearInterval(timer);
    };

    window.smartBarangayTime = {
        formatDate,
        formatDateTime,
        formatCurrentMonthLabel,
        formatCurrentMonthRange,
        formatLiveSync,
        startLiveClock,
        timeZone,
    };
})();
