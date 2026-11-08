#include "CvGameCoreDLLPCH.h"

#include "CivHooks.h"
#include "CivPluginApi.h"
#include "Lua/CvLuaPlot.h"

#include <stdarg.h>
#include <stdio.h>

#define PLUGIN_EXPORT extern "C" __declspec(dllexport)

CIV_PLUGIN_EXPORT_API_VERSION;

static const CivHostInfo* s_host = 0;

// Writes a line to Logs\Bazaar.log, headed with this plugin's name.
static void Log(const char* format, ...)
{
	char line[512];
	va_list args;
	va_start(args, format);
	_vsnprintf_s(line, sizeof(line), _TRUNCATE, format, args);
	va_end(args);
	s_host->Log(s_host->pluginIndex, line);
}

static void Check(int result, const char* hook)
{
	if (result != CIV_HOOK_OK)
		Log("could not register %s (result %d)", hook, result);
}

// ScoreExplorePlot is not told which unit it scores for. ExecuteExplorerMoves checks each tile with
// IsValidExplorerEndTurnPlot just before scoring it, so that check names the unit.
static const CvPlot* s_checkedPlot = 0;
static PlayerTypes s_checkedOwner = NO_PLAYER;
static int s_checkedUnit = -1;

static void AfterIsValidExplorerEndTurnPlot(const CvHomelandAI* homeland, const CvUnit* unit, CvPlot* plot, bool& result)
{
	s_checkedPlot = result ? plot : 0;
	s_checkedOwner = unit->getOwner();
	s_checkedUnit = unit->GetID();
}

// Explorers scored with land sight at sea shuttle forever between coastal tiles that can never reveal the ocean beyond.
static int ReplaceScoreExplorePlot(int (*original)(CvPlot*, TeamTypes, int, DomainTypes), CvPlot* plot, TeamTypes team, int range,
	DomainTypes domain)
{
	const bool checked = plot == s_checkedPlot;
	s_checkedPlot = 0;
	if (checked && domain == DOMAIN_LAND)
	{
		const CvUnit* unit = GET_PLAYER(s_checkedOwner).getUnit(s_checkedUnit);
		// An embarked unit with sight 0 still reveals the tiles next to it.
		if (unit && !plot->isTerraFirma(unit) && !plot->IsAllowsWalkWater())
			range = std::max(1, GC.getEMBARKED_VISIBILITY_RANGE() + unit->GetEmbarkExtraVisibility());
	}
	return original(plot, team, range, domain);
}

// The game never clears its "turn can end" mark mid-turn, so Auto End Turn would skip choices that turn up later, such as
// a new city's production. The test below is updateTestEndTurn's own. Without WAIT_END_TURN, that function may never set
// the mark again once a unit has moved, so the hook stays out.
static bool BeforeUpdateTestEndTurn(CvGame* game)
{
	ICvUserInterface2* ui = GC.GetEngineUserInterface();
	if (!ui || !ui->canEndTurn())
		return false;
	CvPlayer& player = GET_PLAYER(game->getActivePlayer());
	if (!player.isTurnActive() || !player.isOption(PLAYEROPTION_WAIT_END_TURN))
		return false;
	EndTurnBlockingTypes blocking;
	int notification;
	const bool waitingOnPlayer = player.GetNotifications()->GetEndTurnBlockedType(blocking, notification)
		|| (player.hasPromotableUnit() && !game->isOption(GAMEOPTION_PROMOTION_SAVING)) || player.hasReadyUnit();
	if (waitingOnPlayer)
		ui->setCanEndTurn(false);
	return false;
}

struct ProtectedNotifications : CvNotifications
{
	static const std::vector<Notification>& List(const CvNotifications* notifications)
	{
		return notifications->*&ProtectedNotifications::m_aNotifications;
	}
};

// The game's shortage case falls through into the pantheon case, so the warning expires whenever the player can't found a
// pantheon, even while still short.
static void AfterIsNotificationExpired(CvNotifications* notifications, int index, bool& result)
{
	const CvNotifications::Notification& notification = ProtectedNotifications::List(notifications)[index];
	if (notification.m_eNotificationType != NOTIFICATION_DEMAND_RESOURCE)
		return;
	result = notification.m_iX == -1 && notification.m_iY == -1
		&& GET_PLAYER(notification.m_ePlayerID).getNumResourceAvailable((ResourceTypes)notification.m_iGameDataIndex, true) >= 0;
}

struct ProtectedGame : CvGame
{
	static void ClearForceEndingTurn(CvGame* game) { (game->*&ProtectedGame::SetForceEndingTurn)(false); }
};

// Shift+Enter sets this flag, but the game clears it only on finding a unit that needs orders, so it lingers into later turns.
static void AfterGameDoTurn(CvGame* game)
{
	ProtectedGame::ClearForceEndingTurn(game);
}

// Loading puts MAX_MAJOR_CIVS defaults in each list and then appends the saved list, while the game reads only the first
// MAX_MAJOR_CIVS. The entries the game was using when it saved are the first MAX_MAJOR_CIVS of the saved list.
template <class List>
static void KeepSavedEntries(List& list)
{
	if (list.size() < 2 * MAX_MAJOR_CIVS)
		return;
	for (uint i = 0; i < MAX_MAJOR_CIVS; ++i)
		list[i] = list[MAX_MAJOR_CIVS + i];
	while (list.size() > MAX_MAJOR_CIVS)
		list.pop_back();
}

static void AfterPlayerRead(CvPlayer* player, FDataStream& stream)
{
	CvEspionageAI* ai = player->GetEspionageAI();
	KeepSavedEntries(ai->m_aiCivOutOfTechTurn);
	KeepSavedEntries(ai->m_aiNumSpiesCaught);
	KeepSavedEntries(ai->m_aiNumSpiesKilled);
	KeepSavedEntries(ai->m_aiNumSpiesDied);
	KeepSavedEntries(ai->m_aiTurnLastSpyCaught);
	KeepSavedEntries(ai->m_aiTurnLastSpyKilled);
	KeepSavedEntries(ai->m_aiTurnLastSpyDied);
}

static bool AllyHasSpyThere(CvPlayerEspionage* espionage, uint spy)
{
	if (spy >= espionage->m_aSpyList.size())
		return false;
	CvCity* city = espionage->GetCityWithSpy(spy);
	if (!city || !GET_PLAYER(city->getOwner()).isMinorCiv())
		return false;
	const PlayerTypes ally = GET_PLAYER(city->getOwner()).GetMinorCivAI()->GetAlly();
	return ally != NO_PLAYER && city->GetCityEspionage()->m_aiSpyAssignment[ally] != -1;
}

struct ProtectedGlobals : CvGlobals
{
	static float& CoupNobodyBonus() { return GC.*&ProtectedGlobals::m_fESPIONAGE_COUP_NOBODY_BONUS; }
};

static bool s_coupBonusLifted = false;
static float s_coupNobodyBonus = 0;

// The game marks the ally as having no spy here even when it has one, so it always applies the bonus meant for an undefended
// city-state.
static bool BeforeGetCoupChanceLiftBonus(CvPlayerEspionage* espionage, uint spy, int& result)
{
	if (AllyHasSpyThere(espionage, spy))
	{
		s_coupNobodyBonus = ProtectedGlobals::CoupNobodyBonus();
		ProtectedGlobals::CoupNobodyBonus() = 1.0f;
		s_coupBonusLifted = true;
	}
	return false;
}

static void AfterGetCoupChanceRestoreBonus(CvPlayerEspionage* espionage, uint spy, int& result)
{
	if (!s_coupBonusLifted)
		return;
	ProtectedGlobals::CoupNobodyBonus() = s_coupNobodyBonus;
	s_coupBonusLifted = false;
}

static bool s_inAttemptCoup = false;

static bool BeforeAttemptCoup(CvPlayerEspionage* espionage, uint spy, bool& result)
{
	s_inAttemptCoup = true;
	return false;
}

// AttemptCoup succeeds when its roll of 0-99 is at or below the chance, one point more often than the chance says.
static void AfterGetCoupChanceLowerForRoll(CvPlayerEspionage* espionage, uint spy, int& result)
{
	if (!s_inAttemptCoup)
		return;
	s_inAttemptCoup = false;
	--result;
}

// AttemptCoup can return before asking for the chance, so the flag must not outlive it.
static void AfterAttemptCoup(CvPlayerEspionage* espionage, uint spy, bool& result)
{
	s_inAttemptCoup = false;
}

// The game's switch has no breaks, so it reports every tile as 1, "seen before, not visible now".
static void AfterGetActiveFogOfWarMode(lua_State* L, int& pushed)
{
	const CvPlot* plot = CvLuaPlot::GetInstance(L, 1, false);
	if (!plot || pushed != 1)
		return;
	int mode = 0;
	switch (plot->GetActiveFogOfWarMode())
	{
	case FOGOFWARMODE_OFF:
		mode = 2;
		break;
	case FOGOFWARMODE_UNEXPLORED:
		mode = 0;
		break;
	case FOGOFWARMODE_NOVIS:
		mode = 1;
		break;
	}
	lua_pop(L, 1);
	lua_pushinteger(L, mode);
}

PLUGIN_EXPORT bool CivPluginEntry(const CivHostInfo* host, CivPluginInfo* plugin)
{
	s_host = host;
	plugin->name = "Small Bugfix Compendium (Bazaar 0.5)";
	plugin->version = "1";
	Check(CivRegisterAfter<CivHooks::CvHomelandAI_IsValidExplorerEndTurnPlot>(host, &AfterIsValidExplorerEndTurnPlot),
		"after CvHomelandAI::IsValidExplorerEndTurnPlot");
	Check(CivRegisterReplace<CivHooks::CvEconomicAI_ScoreExplorePlot>(host, &ReplaceScoreExplorePlot),
		"replacement of CvEconomicAI::ScoreExplorePlot");
	Check(CivRegisterBefore<CivHooks::CvGame_updateTestEndTurn>(host, &BeforeUpdateTestEndTurn), "before CvGame::updateTestEndTurn");
	Check(CivRegisterAfter<CivHooks::CvNotifications_IsNotificationExpired>(host, &AfterIsNotificationExpired),
		"after CvNotifications::IsNotificationExpired");
	Check(CivRegisterAfter<CivHooks::CvGame_doTurn>(host, &AfterGameDoTurn), "after CvGame::doTurn");
	Check(CivRegisterAfter<CivHooks::CvPlayer_Read>(host, &AfterPlayerRead), "after CvPlayer::Read");
	Check(CivRegisterBefore<CivHooks::CvPlayerEspionage_GetCoupChanceOfSuccess>(host, &BeforeGetCoupChanceLiftBonus),
		"before CvPlayerEspionage::GetCoupChanceOfSuccess (lift bonus)");
	Check(CivRegisterAfter<CivHooks::CvPlayerEspionage_GetCoupChanceOfSuccess>(host, &AfterGetCoupChanceRestoreBonus),
		"after CvPlayerEspionage::GetCoupChanceOfSuccess (restore bonus)");
	Check(CivRegisterBefore<CivHooks::CvPlayerEspionage_AttemptCoup>(host, &BeforeAttemptCoup), "before CvPlayerEspionage::AttemptCoup");
	Check(CivRegisterAfter<CivHooks::CvPlayerEspionage_GetCoupChanceOfSuccess>(host, &AfterGetCoupChanceLowerForRoll),
		"after CvPlayerEspionage::GetCoupChanceOfSuccess (coup roll)");
	Check(CivRegisterAfter<CivHooks::CvPlayerEspionage_AttemptCoup>(host, &AfterAttemptCoup), "after CvPlayerEspionage::AttemptCoup");
	Check(CivRegisterAfter<CivHooks::CvLuaPlot_lGetActiveFogOfWarMode>(host, &AfterGetActiveFogOfWarMode),
		"after CvLuaPlot::lGetActiveFogOfWarMode");
	return true;
}
