export function isWithinWindow(currentHour, startHour, endHour) {
  if (startHour <= endHour) {
    return currentHour >= startHour && currentHour < endHour;
  }
  return currentHour >= startHour || currentHour < endHour;
}
